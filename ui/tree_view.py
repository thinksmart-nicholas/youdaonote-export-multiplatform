# -*- coding: utf-8 -*-
"""笔记列表页：目录树 + 三态勾选 + 搜索 / 全选 / 清空。

实现要点：
- 进入本页后**在后台线程全量加载目录树**到 ``state.tree``（导出必须依赖
  完整树，否则未展开的文件夹不会被导出）；界面仍然**懒渲染**：仅展开
  的文件夹才显示其子节点，避免一次性渲染过多控件。
- 三态 Checkbox：父节点全选=True，部分子项选中=None，均未选=False。
  选中文件夹在导出时等价于选中其下全部笔记（由 ``Exporter`` 负责展开）。
- 搜索：在已加载节点中按名称匹配，扁平展示。
"""
import flet as ft



def build_tree_view(page, state, on_next, on_back):
    search_field = ft.TextField(
        label="搜索（按名称）",
        width=320,
        on_change=lambda e: render(),
    )
    select_all_btn = ft.FilledButton("全选", on_click=lambda e: select_all())
    clear_btn = ft.OutlinedButton("清空", on_click=lambda e: clear_all())
    status = ft.Text("", size=13, color=ft.Colors.GREY)

    tree_container = ft.Column(
        scroll=ft.ScrollMode.AUTO, expand=True, spacing=2
    )
    loading = ft.ProgressRing(visible=True)
    loading_text = ft.Text("正在加载目录树…", color=ft.Colors.GREY)

    # 闭包状态
    expanded = set()          # 已展开的文件夹 id
    parent_map = {}           # node_id -> 父节点
    loaded = False

    def _walk_full(node, parent=None):
        parent_map[node.id] = parent
        for child in node.children:
            _walk_full(child, node)

    def list_files(node):
        """返回 node 下所有笔记（文件）节点，依赖已加载的完整树。"""
        out = []
        for child in node.children:
            if child.is_dir:
                out.extend(list_files(child))
            else:
                out.append(child)
        return out

    def list_all_ids(node):
        """node 自身 + 所有后代（文件夹与笔记）的 id。"""
        ids = [node.id]
        for child in node.children:
            if child.is_dir:
                ids.extend(list_all_ids(child))
            else:
                ids.append(child.id)
        return ids

    def is_selected(node):
        """node 自身或其任一祖先在 selected_ids 中即为选中。"""
        cur = node
        while cur is not None:
            if cur.id in state.selected_ids:
                return True
            cur = parent_map.get(cur.id)
        return False

    def effective_state(node):
        """返回三态：True / False / None（部分选中）。"""
        if not node.is_dir:
            return is_selected(node)
        files = list_files(node)
        if not files:
            # 无子文件（空文件夹或尚未加载）：以自身是否选中判断
            return node.id in state.selected_ids
        n_sel = sum(1 for f in files if is_selected(f))
        if n_sel == 0:
            return False
        if n_sel == len(files):
            return True
        return None

    def toggle(node):
        eff = effective_state(node)
        want_on = eff is not True  # False 或 None 都视为「要点亮」
        if node.is_dir:
            desc = list_all_ids(node)
            for i in desc:
                state.selected_ids.discard(i)
            if want_on:
                state.selected_ids.add(node.id)
        else:
            if want_on:
                state.selected_ids.add(node.id)
            else:
                state.selected_ids.discard(node.id)
        render()

    def select_all():
        if state.tree is not None:
            state.selected_ids = {state.tree.id}
            render()

    def clear_all():
        state.selected_ids.clear()
        render()

    def make_row(node, depth):
        indent = ft.Container(width=depth * 20)
        leading = ft.Container(width=24)
        if node.is_dir:
            is_open = node.id in expanded
            leading = ft.IconButton(
                (ft.Icons.FOLDER_OPEN if is_open else ft.Icons.FOLDER),
                icon_size=18,
                tooltip="展开 / 收起",
                on_click=lambda e, n=node: toggle_expand(n),
            )
        cb = ft.Checkbox(
            tristate=True,
            value=effective_state(node),
            on_change=lambda e, n=node: toggle(n),
        )
        name_text = ft.Text(
            node.name or "(未命名)",
            overflow=ft.TextOverflow.ELLIPSIS,
            expand=True,
        )
        return ft.Row(
            [indent, leading, cb, name_text],
            spacing=4,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def toggle_expand(node):
        if node.id in expanded:
            expanded.discard(node.id)
        else:
            expanded.add(node.id)
        render()

    def render():
        if not loaded or state.tree is None:
            tree_container.controls = [loading, loading_text]
            page.update()
            return

        query = (search_field.value or "").strip().lower()
        controls = []
        if query:
            # 扁平搜索：在已加载节点中按名称匹配
            matches = []

            def collect(n):
                for c in n.children:
                    if query in (c.name or "").lower():
                        matches.append(c)
                    if c.is_dir:
                        collect(c)

            collect(state.tree)
            if not matches:
                controls.append(ft.Text("没有匹配的笔记", color=ft.Colors.GREY))
            for m in matches:
                controls.append(make_row(m, 0))
        else:
            def walk(n, depth):
                for child in n.children:
                    controls.append(make_row(child, depth))
                    if child.is_dir and child.id in expanded:
                        walk(child, depth + 1)

            walk(state.tree, 0)

        tree_container.controls = controls
        sel = sum(
            1 for f in list_files(state.tree) if is_selected(f)
        ) if state.tree else 0
        status.value = "已勾选约 {} 篇笔记（文件夹按整目录计）".format(sel)
        page.update()

    def load_full_tree():
        exporter = state.exporter
        root, err = exporter.load_tree(on_folder_loaded=None)
        if err:
            loading.visible = False
            loading_text.value = "加载目录树失败：{}".format(err)
            loading_text.color = ft.Colors.RED
            page.update()
            return
        state.tree = root
        _walk_full(root, None)
        nonlocal loaded
        loaded = True
        render()

    view = ft.Column(
        expand=True,
        spacing=10,
        controls=[
            ft.Row(
                [ft.Text("选择要导出的笔记", size=20, weight=ft.FontWeight.BOLD),
                 ft.Container(expand=True),
                 ft.TextButton("返回登录", on_click=lambda e: on_back())],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Row([search_field, select_all_btn, clear_btn]),
            ft.Container(
                border=ft.Border.all(1, ft.Colors.OUTLINE),
                border_radius=8,
                padding=8,
                expand=True,
                content=tree_container,
            ),
            status,
            ft.FilledButton(
                "下一步：选择导出目录",
                icon=ft.Icons.ARROW_FORWARD,
                on_click=lambda e: on_next(),
                width=240,
            ),
        ],
    )

    # 进入本页即开始后台加载目录树
    page.run_thread(load_full_tree)
    return view
