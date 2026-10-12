# -*- coding: utf-8 -*-
"""跨平台打包脚本：用 flet pack 生成单文件可执行程序。

用法：
    python build.py            # 在当前平台打包为单文件可执行程序
    python build.py --onedir   # 生成单目录（便于调试依赖问题）
    python build.py --distpath out

说明：
- flet pack 基于 PyInstaller，会把 selenium / flet 运行时一并打进 exe。
- 浏览器驱动不打包进 exe，首次运行由 Selenium Manager 联网下载并缓存。
- 通过 ``--paths`` 把项目根目录加入 PyInstaller 搜索路径，
确保根目录下的 pull / core / ui 等模块被正确收集（否则打包后运行会报 ImportError）。
- 通过 ``--collect-data selenium`` 把 selenium 的数据文件（含各平台的
``selenium-manager.exe`` 二进制）一并打进包；否则打包后运行时找不到
selenium-manager 二进制，浏览器驱动无法下载（默认开启的 npmmirror 加速也因此失效）。
- 镜像加速（npmmirror）在代码中默认开启，无需打包侧额外配置；仅依赖上面
打包进来的 selenium-manager 二进制来实际执行加速下载。
"""
import argparse
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, "ui", "main.py")
APP_NAME = "YoudaoNoteExport"

# 可选图标（文件不存在则忽略）
ICONS = {
    "win32": os.path.join(ROOT, "other", "icon.ico"),
    "darwin": os.path.join(ROOT, "other", "icon.icns"),
}


def main():
    parser = argparse.ArgumentParser(description="打包有道云笔记导出工具")
    parser.add_argument("--onedir", action="store_true",
                        help="生成单目录而非单文件（默认单文件）")
    parser.add_argument("--distpath", default=os.path.join(ROOT, "dist"),
                        help="产物输出目录（默认 ./dist）")
    args = parser.parse_args()

    cmd = [
        "flet", "pack",
        SCRIPT,
        "--name", APP_NAME,
        "--hidden-import", "selenium",
        # selenium 4.x 用 importlib 懒加载 webdriver.Edge / Chrome / Firefox
        # （见 selenium/webdriver/__init__.py 的 _LAZY_IMPORTS），
        # PyInstaller 静态分析扫不到，必须显式声明，否则打包后运行时报
        # "No module named 'selenium.webdriver.edge.webdriver'"。
        "--hidden-import", "selenium.webdriver.edge.webdriver",
        "--hidden-import", "selenium.webdriver.chrome.webdriver",
        "--hidden-import", "selenium.webdriver.firefox.webdriver",
        "--distpath", args.distpath,
        "-y",
    ]
    # --collect-data selenium：把 selenium 的数据文件（含 selenium-manager.exe）
    # 打进包，否则打包后运行时找不到二进制，驱动下载与默认加速均失效。
    # 该参数经 ``--`` 透传给 PyInstaller。
    if args.onedir:
        cmd.append("-D")
    icon = ICONS.get(sys.platform)
    if icon and os.path.isfile(icon):
        cmd += ["--icon", icon]
    # 把项目根目录传给 PyInstaller，确保 pull / core / ui 被收集；
    # 通过 ``--`` 透传，路径含空格也安全。
    # --collect-data selenium：一并打包 selenium 的数据文件（含 selenium-manager.exe 二进制），
    # 否则打包后运行时找不到二进制，浏览器驱动无法下载、默认 npmmirror 加速也失效。
    cmd += ["--", "--paths", ROOT, "--collect-data", "selenium"]

    print("[build] 执行：", " ".join(cmd))
    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
