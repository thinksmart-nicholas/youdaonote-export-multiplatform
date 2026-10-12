# -*- coding: utf-8 -*-
"""有道云笔记导出 · 桌面 GUI 入口。

运行：在项目根目录执行 ``flet run ui/main.py``（首次会拉起桌面窗口，
无需 Flutter SDK；``flet`` 已自带预编译桌面客户端）。

注意：`flet run` 会把脚本所在目录（``ui/``）加入 sys.path，
而核心引擎 ``pull`` / ``core`` 位于项目根目录，因此这里先把项目根
目录插入 sys.path，保证 ``import pull`` / ``import core`` 可用。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import flet as ft

from core.exporter import Exporter
from ui.export_view import build_export_view
from ui.login_view import build_login_view
from ui.tree_view import build_tree_view


class AppState(object):
    """跨页面共享的状态。"""

    def __init__(self):
        self.exporter = Exporter()   # 登录 / 列目录 / 导出 共用引擎
        self.tree = None             # load_tree 得到的完整目录树
        self.selected_ids = set()    # 勾选的节点 id 集合（可含文件夹）
        self.user_name = ""


async def main(page: ft.Page):
    page.title = "有道云笔记导出"
    page.window.width = 980
    page.window.height = 740
    page.window.min_width = 720
    page.window.min_height = 560
    # 窗口默认居中显示
    await page.window.center()

    state = AppState()
    cache = {}

    def switch(key):
        builder = builders[key]
        if key not in cache:
            cache[key] = builder()
        page.controls.clear()
        page.controls.append(cache[key])
        page.update()

    builders = {
        "login": lambda: build_login_view(page, state, lambda: switch("tree")),
        "tree": lambda: build_tree_view(
            page, state,
            on_next=lambda: switch("export"),
            on_back=lambda: switch("login"),
        ),
        "export": lambda: build_export_view(
            page, state, on_back=lambda: switch("tree")
        ),
    }

    switch("login")


if __name__ == "__main__":
    ft.run(main)
