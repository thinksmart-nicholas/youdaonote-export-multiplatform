# -*- coding: utf-8 -*-
"""导出引擎门面。

把「登录 / 列目录 / 选择性导出」封装成与界面无关的 API，
供命令行（pull.py）与图形界面共用同一套引擎，避免逻辑分裂。

设计要点：

- **不复制任何转换逻辑**。单文件处理直接复用
  `pull.YoudaoNotePull._add_or_update_file()`，因此格式转换、图片迁移、
  白板渲染、时间回填等行为与命令行完全一致，不存在两份实现。
- **遍历与勾选**是界面特有的需求（选择性导出），放在本模块，
  不塞进 pull.py，保持命令行入口的行为稳定。
- 所有可能失败的操作统一返回 ``(结果, error_msg)``，
  而不是抛异常，方便界面直接把 error_msg 展示给用户。
"""
import logging
import os
import re

from core.api import YoudaoNoteApi
from pull import FileActionEnum, YoudaoNotePull


class NoteNode(object):
    """云端目录树的一个节点（文件夹或笔记）"""

    __slots__ = ("id", "name", "is_dir", "create_time", "modify_time", "children")

    def __init__(self, id="", name="", is_dir=False, create_time=0, modify_time=0):
        self.id = id
        self.name = name
        self.is_dir = is_dir
        self.create_time = create_time
        self.modify_time = modify_time
        self.children = []  # 仅文件夹会填充

    def __repr__(self):
        return "<NoteNode {}:{}>".format("D" if self.is_dir else "F", self.name)


class ExportReport(object):
    """一次导出的结果统计"""

    def __init__(self):
        self.added = 0
        self.updated = 0
        self.skipped = 0
        self.failed = 0
        self.failures = []  # [(笔记名, 错误提示)]
        self.cancelled = False

    def __str__(self):
        text = "新增 {}，更新 {}，跳过 {}，失败 {}".format(
            self.added, self.updated, self.skipped, self.failed
        )
        if self.cancelled:
            text += "（已取消）"
        return text


class Exporter(object):
    """导出引擎：登录、列目录、按勾选导出"""

    #: 登录必需的 Cookie 字段
    REQUIRED_COOKIE_NAMES = ("YNOTE_CSTK",)

    def __init__(self):
        self.api = YoudaoNoteApi()
        # 复用命令行那套单文件处理逻辑（转换 / 图片 / 白板 / 时间回填）
        self.pull = YoudaoNotePull()
        self.pull.youdaonote_api = self.api

    # ------------------------------------------------------------------
    # 登录与配置
    # ------------------------------------------------------------------
    def login(self, cookies) -> str:
        """装载 Cookie 完成登录

        :param cookies: [[name, value, domain, path], ...]
        :return: error_msg，空字符串表示成功
        """
        return self.api.set_cookies(cookies)

    def login_from_file(self, cookies_path=None) -> str:
        """从 cookies.json 登录（命令行与「从文件导入」场景）"""
        if cookies_path:
            self.api.cookies_path = cookies_path
        try:
            cookies = self.api._covert_cookies()
        except Exception as err:
            return format(err)
        return self.login(cookies)

    def configure(self, smms_secret_token="", is_relative_path=True, is_base64_image=True):
        """设置导出选项

        :param smms_secret_token: SM.MS 图床 Token，留空则不上传
        :param is_relative_path: 附件是否使用相对路径
        :param is_base64_image: 图片是否内嵌为 base64
        """
        self.pull.smms_secret_token = smms_secret_token
        self.pull.is_relative_path = is_relative_path
        self.pull.is_base64_image = is_base64_image

    # ------------------------------------------------------------------
    # 目录树
    # ------------------------------------------------------------------
    def get_root_id(self):
        """获取根目录 ID

        Cookie 失效时有道返回的是 HTTP 500 + 错误体（没有 fileEntry），
        这里转成可读提示，避免界面上抛出 KeyError。

        :return: (root_id, error_msg)
        """
        info = self.api.get_root_dir_info_id()
        file_entry = info.get("fileEntry") if isinstance(info, dict) else None
        if not file_entry or not file_entry.get("id"):
            return "", "获取根目录失败，接口返回：{}\n常见原因是 Cookie 已过期，请重新登录后更新".format(info)
        return file_entry["id"], ""

    def list_dir(self, dir_id):
        """列出某个目录的直接子项（单层）

        目前仅由 ``_fill_children()`` 递归调用以逐级填充目录树；
        界面已改为一次性 ``load_tree()`` 全量加载，不再按需懒加载。

        :return: (nodes, error_msg)
        """
        info = self.api.get_dir_info_by_id(dir_id)
        if not isinstance(info, dict) or "entries" not in info:
            return [], "接口返回中没有 entries 字段，可能是 Cookie 已过期或接口已变更：{}".format(info)

        nodes = []
        for entry in info.get("entries") or []:
            file_entry = entry.get("fileEntry") or {}
            nodes.append(
                NoteNode(
                    id=file_entry.get("id", ""),
                    name=file_entry.get("name", ""),
                    is_dir=bool(file_entry.get("dir")),
                    create_time=int(file_entry.get("createTimeForSort") or 0),
                    modify_time=int(file_entry.get("modifyTimeForSort") or 0),
                )
            )
        return nodes, ""

    def load_tree(self, root_id=None, on_folder_loaded=None):
        """全量加载目录树

        :param root_id: 起始目录 ID，留空则从根目录开始
        :param on_folder_loaded: 每加载完一个文件夹回调一次，便于界面渐进显示
        :return: (root_node, error_msg)
        """
        if not root_id:
            root_id, error_msg = self.get_root_id()
            if error_msg:
                return None, error_msg

        root = NoteNode(id=root_id, name="", is_dir=True)
        error_msg = self._fill_children(root, on_folder_loaded)
        if error_msg:
            return None, error_msg
        return root, ""

    def _fill_children(self, node, on_folder_loaded=None):
        """递归填充子节点，返回 error_msg"""
        children, error_msg = self.list_dir(node.id)
        if error_msg:
            return error_msg
        node.children = children
        if on_folder_loaded:
            on_folder_loaded(node)
        for child in children:
            if child.is_dir:
                error_msg = self._fill_children(child, on_folder_loaded)
                if error_msg:
                    return error_msg
        return ""

    # ------------------------------------------------------------------
    # 选择集
    # ------------------------------------------------------------------
    @staticmethod
    def collect_selected_files(tree, selected_ids):
        """把「选中项」展开为真正需要导出的笔记 ID 集合

        选中文件夹等价于选中其下所有笔记，因此这里向下展开。

        :param tree: load_tree() 的根节点
        :param selected_ids: 界面勾选的节点 ID 集合
        :return: set of 笔记 ID
        """
        selected_ids = set(selected_ids or ())
        result = set()

        def walk(node, ancestor_selected):
            take = ancestor_selected or (node.id in selected_ids)
            if node.is_dir:
                for child in node.children:
                    walk(child, take)
            elif take:
                result.add(node.id)

        walk(tree, False)
        return result

    @staticmethod
    def _has_selected(node, selected):
        """该节点（含子孙）是否存在需要导出的笔记"""
        if not node.is_dir:
            return node.id in selected
        return any(Exporter._has_selected(child, selected) for child in node.children)

    @staticmethod
    def _count_selected(node, selected):
        if not node.is_dir:
            return 1 if node.id in selected else 0
        return sum(Exporter._count_selected(child, selected) for child in node.children)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def export(self, tree, selected_ids, dest_dir, on_progress=None, cancel_event=None):
        """按勾选导出笔记

        :param tree: load_tree() 返回的目录树
        :param selected_ids: 勾选的节点 ID 集合（可含文件夹）
        :param dest_dir: 导出目标目录（由用户自选）
        :param on_progress: 进度回调 ``(done, total, current_name, skipped)``
        :param cancel_event: ``threading.Event``，置位后中断导出
        :return: ExportReport
        """
        report = ExportReport()
        selected = self.collect_selected_files(tree, selected_ids)
        total = self._count_selected(tree, selected)

        dest_dir = os.path.abspath(dest_dir)
        os.makedirs(dest_dir, exist_ok=True)
        # ImagePull 需要知道导出根目录，用于计算相对路径
        self.pull.root_local_dir = dest_dir

        state = {"done": 0}

        def tick(name, skipped=False, updated=False):
            state["done"] += 1
            if on_progress:
                on_progress(state["done"], total, name, skipped, updated)

        self._export_node(tree, dest_dir, selected, report, tick, cancel_event)

        if cancel_event is not None and cancel_event.is_set():
            report.cancelled = True
        logging.info("导出完成：%s", report)
        return report

    def _export_node(self, node, dest_dir, selected, report, tick, cancel_event):
        """递归导出一个节点"""
        if cancel_event is not None and cancel_event.is_set():
            return

        if node.is_dir:
            if not self._has_selected(node, selected):
                return
            # 文件夹名同样需要清理非法字符（有道允许的名称本地可能不合法）
            sub_dir = os.path.join(dest_dir, self.pull._optimize_file_name(node.name))
            os.makedirs(sub_dir, exist_ok=True)
            for child in node.children:
                self._export_node(child, sub_dir, selected, report, tick, cancel_event)
                if cancel_event is not None and cancel_event.is_set():
                    return
            return

        if node.id not in selected:
            return

        action, _, error_msg = self.pull._add_or_update_file(
            node.id, node.name, dest_dir, node.modify_time, node.create_time
        )
        skipped = False
        updated = False
        if error_msg:
            report.failed += 1
            report.failures.append((node.name, error_msg))
        elif action == FileActionEnum.CONTINUE:
            report.skipped += 1
            skipped = True
        elif action == FileActionEnum.UPDATE:
            report.updated += 1
            updated = True
        else:
            report.added += 1
        tick(node.name, skipped, updated)
