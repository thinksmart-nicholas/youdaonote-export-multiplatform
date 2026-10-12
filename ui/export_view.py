# -*- coding: utf-8 -*-
"""导出页：选择目录 + 选项 + 进度 + 取消 + 结果报告。

- 目标目录：``FilePicker.get_directory_path()``（用户自选，不使用默认目录）。
- 选项开关：``is_base64_image`` / ``is_relative_path`` → ``Exporter.configure()``。
- 导出在后台线程执行，进度回调里 ``page.update()``，避免界面卡死。
- 导出进行中设置 ``page.window.prevent_close = True``，并拦截关窗事件触发取消。
"""
import threading

import flet as ft


def build_export_view(page, state, on_back):
    dest_text = ft.Text("未选择导出目录", color=ft.Colors.GREY)
    base64_cb = ft.Checkbox(label="图片内嵌为 base64", value=True)
    rel_cb = ft.Checkbox(label="附件使用相对路径", value=True)
    progress = ft.ProgressBar(width=560, visible=False)
    progress_text = ft.Text("", size=13)
    log = ft.ListView(expand=True, spacing=2, padding=4, auto_scroll=True)
    report_text = ft.Text("", size=14, selectable=True)
    cancel_btn = ft.FilledButton(
        "取消导出", icon=ft.Icons.STOP, disabled=True, on_click=lambda e: do_cancel()
    )
    export_btn = ft.FilledButton(
        "开始导出", icon=ft.Icons.DOWNLOAD, width=200, on_click=lambda e: on_export_click(e)
    )

    # 后台导出所需的取消信号与状态
    cancel_event = threading.Event()
    exporting = {"active": False}

    dir_picker = ft.FilePicker()
    # Flet 1.0 起 FilePicker 属于“服务”，必须放入 page.services；
    # 放入 page.overlay 会被当成 UI 控件渲染，报 Unknown control: FilePicker
    page.services.append(dir_picker)
    dest_dir = {"path": ""}

    async def on_choose_dir(e):
        # 注意：Flet 1.0 中 get_directory_path 是 async 方法，必须 await；
        # 返回值直接是路径字符串（取消则为 None），不经过 on_result 回调
        path = await dir_picker.get_directory_path(dialog_title="选择导出目录")
        if path:
            dest_dir["path"] = path
            dest_text.value = path
            dest_text.color = ft.Colors.PRIMARY
        else:
            dest_text.value = "未选择导出目录"
            dest_text.color = ft.Colors.GREY
        page.update()

    def do_cancel():
        cancel_event.set()

    def on_export_click(e):
        if not dest_dir["path"]:
            dest_text.value = "请先点击「选择目录」选定导出位置"
            dest_text.color = ft.Colors.RED
            page.update()
            return
        if state.tree is None:
            report_text.value = "目录树尚未加载，请返回上一步"
            report_text.color = ft.Colors.RED
            page.update()
            return
        if not state.selected_ids:
            report_text.value = "没有勾选任何笔记，请返回选择"
            report_text.color = ft.Colors.RED
            page.update()
            return

        cancel_event.clear()
        exporting["active"] = True
        progress.visible = True
        progress.value = 0
        progress_text.value = "导出中…"
        log.controls.clear()
        report_text.value = ""
        cancel_btn.disabled = False
        export_btn.disabled = True
        page.window.prevent_close = True
        page.update()
        page.run_thread(lambda: do_export())

    async def _page_update():
        page.update()

    async def _apply_progress(done, total, name, skipped, updated):
        # 在 Flet 事件循环线程里变更控件并刷新，才能真正实时推送到客户端；
        # 在后台 worker 线程里直接 page.update() 往往只会在结束时才渲染一次
        if total:
            progress.value = done / total
        progress_text.value = "({}/{}) {}".format(done, total, name)
        if skipped:
            prefix, color = "跳过：", ft.Colors.GREY
        elif updated:
            prefix, color = "更新：", ft.Colors.BLUE
        else:
            prefix, color = "导出：", None
        log.controls.append(ft.Text(prefix + name, size=12, color=color))
        # 限制日志控件长度，避免无限增长拖慢每次刷新
        if len(log.controls) > 300:
            del log.controls[: len(log.controls) - 300]
        page.update()

    def on_progress(done, total, name, skipped=False, updated=False):
        # 把 UI 更新调度回事件循环，确保「导出一条显示一条」实时刷新
        # 注意：run_task 接收「协程函数 + 参数」，而非已创建的协程对象
        page.run_task(_apply_progress, done, total, name, skipped, updated)

    def do_export():
        exporter = state.exporter
        exporter.configure(
            is_relative_path=bool(rel_cb.value),
            is_base64_image=bool(base64_cb.value),
        )
        try:
            report = exporter.export(
                state.tree,
                state.selected_ids,
                dest_dir["path"],
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        except Exception as ex:
            report_text.value = "导出异常：{}".format(ex)
            report_text.color = ft.Colors.RED
        else:
            msg = str(report)
            report_text.value = "导出完成：{}".format(msg)
            report_text.color = (
                ft.Colors.ORANGE if report.cancelled else ft.Colors.GREEN
            )
            if report.failures:
                report_text.value += "\n失败明细："
                for name, err in report.failures:
                    report_text.value += "\n - {}: {}".format(name, err)
        finally:
            exporting["active"] = False
            progress.visible = False
            cancel_btn.disabled = True
            export_btn.disabled = False
            page.window.prevent_close = False
            page.run_task(_page_update)

    def on_window_event(e):
        # 关闭窗口时若正在导出，触发取消而非直接退出
        if e.data == "close" and exporting["active"]:
            cancel_event.set()

    page.window.on_event = on_window_event

    view = ft.Column(
        expand=True,
        spacing=12,
        controls=[
            ft.Row(
                [ft.Text("导出设置", size=20, weight=ft.FontWeight.BOLD),
                 ft.Container(expand=True),
                 ft.TextButton("返回列表", on_click=lambda e: on_back())],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
            ),
            ft.Row(
                [ft.FilledButton("选择目录", icon=ft.Icons.FOLDER_OPEN,
                                   on_click=on_choose_dir),
                 dest_text],
                alignment=ft.MainAxisAlignment.START,
            ),
            ft.Row([base64_cb, rel_cb]),
            ft.Row([progress, progress_text]),
            ft.Container(
                border=ft.Border.all(1, ft.Colors.OUTLINE),
                border_radius=8,
                padding=4,
                height=180,
                content=log,
            ),
            report_text,
            ft.Row([export_btn, cancel_btn]),
        ],
    )
    return view
