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
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(ROOT, "ui", "main.py")
APP_NAME = "YoudaoNoteExporter"

# Linux 下程序窗口的身份（X11 的 WM_CLASS / Wayland 的 app_id）。
#
# flet 的预编译桌面客户端（flet-desktop）把应用 ID 固化为 `com.appveyor.flet`，
# GTK 据此派生窗口的 WM_CLASS 与 app_id；实测设置 FLET_APP_ID 环境变量并不会
# 改变它（客户端自己覆盖了 prgname）。因此 `.desktop` 的 StartupWMClass 必须写
# 这个真实值，Dock / 任务栏才能把运行中的窗口关联到本条目并显示正确图标。
#
# 核对实际值的方法：
#   X11    ：xprop -name "YoudaoNoteExporter" WM_CLASS
#   Wayland：./YoudaoNoteExporter 2>&1 | head   # 看 GLib 告警前缀 **(xxx:PID): WARNING **
#
# 若 flet 将来改变该固化值，构建时用环境变量覆盖即可，无需改代码：
#   YOUDAO_WM_CLASS=<新值> python3 build.py
WM_CLASS = os.environ.get("YOUDAO_WM_CLASS", "com.appveyor.flet")

# 可选图标（文件不存在则忽略）
ICONS = {
    "win32": os.path.join(ROOT, "other", "icon.ico"),
    "darwin": os.path.join(ROOT, "other", "icon.icns"),
}


def _resolve_flet():
    """返回调用 flet CLI 的命令前缀（列表）。

    flet 命令行由 flet 包自身提供（入口点 ``flet = flet.cli:main``）。
    但虚拟环境未激活、或用 ``--user`` 安装、或运行 build.py 的 python 与装依赖的
    pip 不是同一个环境时，该脚本可能不在 PATH 上，直接写 ``"flet"`` 会抛
    ``FileNotFoundError: [Errno 2] No such file or directory: 'flet'``。

    按可靠性依次回退：
      1) PATH 上的 flet
      2) 当前解释器环境（sys.prefix）里的 flet 脚本
      3) 用当前解释器直接执行 ``flet.cli:main``（只要 flet 可导入就一定可用）
    """
    import shutil

    exe = shutil.which("flet")
    if exe:
        return [exe]

    # 当前环境的 Scripts/（Windows）或 bin/（POSIX）
    is_win = os.name == "nt"
    cand = os.path.join(sys.prefix, "Scripts" if is_win else "bin",
                        "flet.exe" if is_win else "flet")
    if os.path.isfile(cand):
        return [cand]

    # 兜底：直接用当前解释器执行 CLI 入口，不再依赖任何外部脚本
    probe = subprocess.call(
        [sys.executable, "-c", "import flet.cli"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if probe != 0:
        raise SystemExit(
            "未找到 flet 命令行工具，且当前 Python（{0}）无法导入 flet。\n"
            "请检查：\n"
            "  1) 是否激活了安装依赖的虚拟环境：source venv/bin/activate\n"
            "  2) 运行 build.py 的 python 与安装依赖的 pip 是否同一个环境\n"
            "  3) 依赖是否装全：pip install -r requirements.txt\n"
            "     （flet 1.x 的桌面客户端在独立包 flet-desktop，打包与 flet run 都需要）"
            .format(sys.executable))
    return [sys.executable, "-c",
            "import sys; from flet.cli import main; sys.exit(main())"]


def main():
    # 强制 stdout / stderr 使用 UTF-8：Windows CI 或英文区域下控制台默认编码为 cp1252，
    # 打印中文（如「[build] 执行：」）会抛 UnicodeEncodeError 导致构建失败。
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

    parser = argparse.ArgumentParser(description="YoudaoNoteExporter 打包工具")
    parser.add_argument("--onedir", action="store_true",
                        help="生成单目录而非单文件（默认单文件）")
    parser.add_argument("--distpath", default=os.path.join(ROOT, "dist"),
                        help="产物输出目录（默认 ./dist）")
    args = parser.parse_args()

    cmd = _resolve_flet() + [
        "pack",
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

    # Linux 下额外生成一个 .desktop 启动器，与可执行文件一同交付，
    # 用户把它和可执行文件放在同一目录即可在桌面环境添加到启动器。
    # （PyInstaller / flet pack 不会自动生成 .desktop，需手动补。）
    if sys.platform.startswith("linux"):
        _write_desktop(args.distpath, APP_NAME)
        _copy_hicolor(args.distpath)


def _copy_hicolor(dest_dir):
    """把 other/icons/hicolor 多尺寸图标树拷到 <dest>/icons/hicolor。

    Linux 的图标既不嵌进可执行文件，也不会因为 .desktop 放在程序旁边就生效：
    .desktop 里写的是「图标名」，桌面环境只从 hicolor 主题目录按尺寸查取。
    因此必须把这些尺寸安装到主题目录（见 tools/install-linux.sh），
    启动器才会显示正确图标，否则是通用占位图。
    """
    src = os.path.join(ROOT, "other", "icons", "hicolor")
    if not os.path.isdir(src):
        print("[build] 跳过图标树：未找到", src)
        return
    dst = os.path.join(dest_dir, "icons", "hicolor")
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print("[build] 已拷贝图标树：", dst)


def _write_desktop(dest_dir, app_name):
    """在 dest_dir 下生成 <app_name>.desktop 启动器（仅 Linux 使用）。"""
    path = os.path.join(dest_dir, "{}.desktop".format(app_name))
    content = (
        "[Desktop Entry]\n"
        "Version=1.0\n"
        "Type=Application\n"
        "Name=YoudaoNoteExporter\n"
        "Comment=YoudaoNoteExporter - 有道云笔记批量导出工具\n"
        "Exec={app}\n"
        "Icon={app}\n"
        "StartupWMClass={wm}\n"
        "Terminal=false\n"
        "Categories=Utility;Office;\n"
    ).format(app=app_name, wm=WM_CLASS)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    # .desktop 需可执行位才会被部分桌面环境识别为启动器
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass
    print("[build] 已生成启动器：", path)


if __name__ == "__main__":
    main()
