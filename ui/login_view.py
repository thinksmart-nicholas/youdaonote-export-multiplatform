# -*- coding: utf-8 -*-
"""登录页：数据库优先 + Selenium 网页登录兜底。

流程（对应 HANDOFF Phase 2 登录页需求）：
1. 用户输入用户名（仅作为区分账户的标识，不需要密码）。
2. 先查 SQLite：若有同名记录，读取其中 cstk/login/sess 三列对应的
   cookies 文本，重组成 cookies 列表后调用 ``Exporter.login`` 登录，
   并用 ``get_root_id`` 验证是否仍有效。
3. 若数据库无记录，或保存的 Cookie 已失效（登录/验证失败），则提示
   「首次登录 / Cookie 已过期」，调用 Selenium 打开有道云笔记网页，
   让用户手动完成一次网页登录；登录成功后从浏览器 cookie 中拆解出
   YNOTE_CSTK / YNOTE_LOGIN / YNOTE_SESS，按 cookies.json 单条格式
   与用户名一并写入数据库，随后用新 Cookie 登录。
"""
import os
import re
import shutil
import subprocess
import sys
import time

import flet as ft

from ui import db

#: 有道云笔记网页端地址；未登录会跳转到登录页，登录后写入授权 cookie
YOUDAO_WEB_URL = "https://note.youdao.com/web/"
REQUIRED_COOKIE_NAMES = ("YNOTE_CSTK", "YNOTE_LOGIN", "YNOTE_SESS")

#: 账号输入框只接受邮箱：与接口返回的真实邮箱做比对
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _candidate_hit(cand):
    """PATH 命令名或绝对路径命中其一即算找到。"""
    return bool(shutil.which(cand) or os.path.isfile(cand))


def _selenium_cache_dir():
    """返回 Selenium Manager 0.4.x 的缓存根目录。

    实测 selenium-manager 0.4.44 跨平台统一使用 ``~/.cache/selenium``
    （Windows 上即 ``C:\\Users\\<user>\\.cache\\selenium``，而非 %LOCALAPPDATA%）。
    ``se-config.toml`` 固定位于该根目录，改 ``SE_cache-path`` 即改其位置。
    """
    return os.path.expanduser(os.path.join("~", ".cache", "selenium"))


def _is_driver_cached(browser):
    """粗略判断对应驱动是否已下载到 Selenium Manager 缓存目录。

    仅用于给出更准确的 UI 提示；判断不准也无妨（最坏情况提示略有偏差）。
    """
    sub = {"edge": "msedgedriver", "chrome": "chromedriver", "firefox": "geckodriver"}.get(browser)
    if not sub:
        return False
    d = os.path.join(_selenium_cache_dir(), sub)
    if not os.path.isdir(d):
        return False
    try:
        return any(os.path.isdir(os.path.join(d, name)) for name in os.listdir(d))
    except Exception:
        return False


# 各浏览器驱动在 npmmirror 二进制仓库中的包名。
# driver-mirror-url 必须包含该包名，因为 Selenium Manager 只会直接拼接
# <版本>/<文件名>，不会自动插入驱动名。
_DRIVER_MIRROR_PKG = {
    "edge": "edgedriver",
    "chrome": "chrome-for-testing",
    "firefox": "geckodriver",
}


def _compute_driver_mirror(browser):
    """返回该浏览器驱动对应的 npmmirror 镜像基础地址；不启用则返回 None。

    基于 selenium-manager 0.4.44 实测结论：
    - 镜像键为统一的 ``driver-mirror-url``，值必须是「含驱动包名的基础路径」，
      例如 ``https://registry.npmmirror.com/-/binary/edgedriver/``；Selenium Manager
      会在其后直接拼接 ``<版本>/<文件名>``，*不会*自动插入驱动名。
    - npmmirror 仅有驱动文件、**没有 LATEST_RELEASE 版本索引**，因此解析时必须显式
      传入已装浏览器的精确版本（见 ``_resolve_driver_path``），否则版本探测会失败。
    - Firefox 的 geckodriver 在 npmmirror 仅提供 ``.zip``（SM 默认请求 ``.tar.gz``），
      镜像会 404，故 Firefox 不启用镜像、回退官方源。

    行为：
    - 默认走 npmmirror（Edge / Chrome 已验证可加速下载，驱动仍只下载一次并缓存）；
    - ``YOUDAO_DRIVER_MIRROR=off`` / ``none`` / ``0`` / ``false`` → 关闭镜像，用官方源；
    - ``YOUDAO_DRIVER_MIRROR=<url根>`` → 自定义镜像（其下需包含
      ``edgedriver`` / ``chrome-for-testing`` / ``geckodriver`` 子目录）；
    - 用户已手动设置 ``SE_driver-mirror-url`` / ``SE_proxy`` 等时完全不干预。
    """
    manual = any(
        os.environ.get(k)
        for k in ("SE_driver-mirror-url", "SE_browser-mirror-url", "SE_proxy")
    )
    raw = (os.environ.get("YOUDAO_DRIVER_MIRROR") or "").strip()
    if raw.lower() in ("off", "none", "0", "false") or manual:
        return None
    pkg = _DRIVER_MIRROR_PKG.get(browser)
    if not pkg or browser == "firefox":
        return None
    base = raw or "https://registry.npmmirror.com/-/binary"
    return base.rstrip("/") + "/" + pkg + "/"


def _get_file_version(path):
    """读取 Windows 可执行文件的「文件版本资源」，返回 ``a.b.c.d``；不支持时返回 None。

    比 ``<exe> --version``（浏览器已运行时只打开会话而不输出版本）和注册表（键存在与否
    依赖浏览器状态）都要稳定，是获取浏览器精确版本号的可靠来源。
    """
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return None
    if not hasattr(ctypes, "windll"):
        return None
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if not size:
            return None
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, None, size, buf):
            return None

        class VS_FIXEDFILEINFO(ctypes.Structure):
            _fields_ = [
                ("dwSignature", wintypes.DWORD),
                ("dwStrucVersion", wintypes.DWORD),
                ("dwFileVersionMS", wintypes.DWORD),
                ("dwFileVersionLS", wintypes.DWORD),
                ("dwProductVersionMS", wintypes.DWORD),
                ("dwProductVersionLS", wintypes.DWORD),
                ("dwFileFlagsMask", wintypes.DWORD),
                ("dwFileFlags", wintypes.DWORD),
                ("dwFileOS", wintypes.DWORD),
                ("dwFileType", wintypes.DWORD),
                ("dwFileSubtype", wintypes.DWORD),
                ("dwFileDateMS", wintypes.DWORD),
                ("dwFileDateLS", wintypes.DWORD),
            ]

        p = ctypes.POINTER(VS_FIXEDFILEINFO)()
        u = wintypes.UINT()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(p), ctypes.byref(u)):
            return None
        ffi = p.contents
        return "{}.{}.{}.{}".format(
            (ffi.dwFileVersionMS >> 16) & 0xFFFF,
            (ffi.dwFileVersionMS) & 0xFFFF,
            (ffi.dwFileVersionLS >> 16) & 0xFFFF,
            (ffi.dwFileVersionLS) & 0xFFFF,
        )
    except Exception:
        return None


def _get_browser_version(browser):
    """获取已安装浏览器的精确版本号，用于 selenium-manager 的 ``--driver-version``。

    npmmirror 缺少版本索引，必须显式给出版本才能跳过版本探测、直接走镜像下载。
    优先读取浏览器 exe 的「文件版本资源」（Windows，最稳定），依次回退到注册表
    （Windows）、``<exe> --version``（跨平台通用）。
    返回形如 ``155.0.4283.45`` 的字符串；取不到则返回 None（此时放弃镜像，走官方源）。

    候选命令名 / 路径按平台区分，确保 Linux/macOS 也能定位到浏览器可执行文件
    （Windows 上 Edge=msedge、Chrome=chrome；Linux 上 Edge=microsoft-edge、Chrome=google-chrome；
    macOS 上为各 App 的二进制路径）。
    """
    import re

    # 各平台浏览器可执行文件候选（命令名 + 常见安装路径），与 _detect_browser 保持一致
    if sys.platform == "win32":
        names = {"edge": ["msedge"], "chrome": ["chrome", "chromium"], "firefox": ["firefox"]}
        guesses = {
            "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                     r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
            "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                       r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
            "firefox": [r"C:\Program Files\Mozilla Firefox\firefox.exe"],
        }.get(browser, [])
    elif sys.platform == "darwin":
        names = {"edge": ["msedge"], "chrome": ["google-chrome", "chromium"], "firefox": ["firefox"]}
        guesses = {
            "edge": ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
            "chrome": ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
            "firefox": ["/Applications/Firefox.app/Contents/MacOS/firefox"],
        }.get(browser, [])
    else:  # linux 及其他类 Unix
        names = {"edge": ["microsoft-edge", "microsoft-edge-stable"],
                 "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
                 "firefox": ["firefox"]}
        guesses = {
            "edge": ["/usr/bin/microsoft-edge", "/opt/microsoft/msedge/msedge"],
            "chrome": ["/usr/bin/google-chrome"],
            "firefox": ["/usr/bin/firefox"],
        }.get(browser, [])

    exe = None
    for n in names.get(browser, []):
        exe = shutil.which(n)
        if exe:
            break
    if not exe:
        for g in guesses:
            if os.path.isfile(g):
                exe = g
                break
    if not exe:
        return None

    # 1) Windows 文件版本资源（仅 Windows 有效，其余平台返回 None）
    ver = _get_file_version(exe)
    if ver:
        return ver

    # 2) 注册表（仅 Windows）
    ver = _get_version_from_registry(browser)
    if ver:
        return ver

    # 3) <exe> --version（跨平台通用回退；Edge 已运行时可能只打开会话而不输出版本，此时忽略）
    try:
        out = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=15
        ).stdout
        m = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _get_version_from_registry(browser):
    """从 Windows 注册表读取浏览器版本号；取不到返回 None。"""
    try:
        import winreg
    except Exception:
        return None

    keys = {
        "edge": [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Edge\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Edge\BLBeacon"),
        ],
        "chrome": [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Google Chrome"),
        ],
        "firefox": [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Mozilla\Mozilla Firefox"),
            (winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Mozilla Firefox"),
        ],
    }.get(browser, [])
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as k:
                for val in ("version", "CurrentVersion", "DisplayVersion"):
                    try:
                        v, _ = winreg.QueryValueEx(k, val)
                        if v and re.match(r"^\d+\.\d+", str(v)):
                            return str(v)
                    except Exception:
                        continue
        except Exception:
            continue
    return None


def _resolve_driver_path(browser):
    """解析浏览器驱动路径：优先用 npmmirror 镜像加速下载（Edge/Chrome），已缓存则复用。

    直接调用 selenium-manager 二进制并显式传入 ``--driver-version`` 与 ``--driver-mirror-url``，
    绕开 selenium 默认调用不会带镜像参数的限制，同时规避 npmmirror 缺版本索引的问题。
    返回驱动可执行文件的绝对路径，供 ``Service(executable_path=...)`` 使用。
    """
    from selenium.webdriver.common.selenium_manager import SeleniumManager

    args = ["--browser", browser]
    # Firefox 的 geckodriver 版本独立于 Firefox 浏览器版本，不能传浏览器版本，
    # 交由 Selenium Manager 自行解析；Edge/Chrome 的驱动版本与浏览器版本一致，可显式给定。
    if browser != "firefox":
        ver = _get_browser_version(browser)
        if ver:
            args += ["--driver-version", ver]
    mirror = _compute_driver_mirror(browser)
    if mirror:
        args += ["--driver-mirror-url", mirror]
    if not getattr(sys, "frozen", False):
        print("[selenium] 解析驱动：browser={} version={} mirror={}".format(
            browser, _get_browser_version(browser) if browser != "firefox" else "(auto)", mirror))
    info = SeleniumManager().binary_paths(args)
    return info["driver_path"]


def _detect_browser():
    """跨平台探测本机已安装的浏览器。

    优先 Chromium 系（Edge / Chrome，cookie 与选项处理一致），其次 Firefox。
    返回 'edge' / 'chrome' / 'firefox' / None。
    """
    if sys.platform == "win32":
        return _detect_browser_windows()
    if sys.platform == "darwin":
        return _detect_browser_mac()
    return _detect_browser_linux()


def _detect_browser_windows():
    # 1) PATH 命令名 + 常见默认安装路径
    path_names = {
        "edge": ["msedge"],
        "chrome": ["chrome", "chromium"],
        "firefox": ["firefox"],
    }
    known = {
        "edge": [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
        "firefox": [r"C:\Program Files\Mozilla Firefox\firefox.exe"],
    }
    for browser in ("edge", "chrome", "firefox"):
        if any(_candidate_hit(n) for n in path_names[browser]):
            return browser
        if any(os.path.isfile(p) for p in known[browser]):
            return browser
    # 2) 注册表：覆盖非默认安装路径
    return _windows_registry_browser()


def _windows_registry_browser():
    """扫描注册表定位浏览器（含非默认安装）。返回浏览器类型或 None。"""
    import importlib
    # 动态导入：winreg 仅 Windows 存在；静态 import 会导致 macOS/Linux 打包时
    # 因找不到模块而失败，动态导入可让 PyInstaller 不尝试收集它（Windows 上 winreg 为内置模块）
    winreg = importlib.import_module("winreg")
    keywords = {
        "edge": ["microsoft edge"],
        "chrome": ["google chrome", "chromium"],
        "firefox": ["mozilla firefox", "firefox"],
    }
    # 2.1 App Paths：直接给出可执行文件绝对路径
    app_paths_root = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    app_map = {"msedge.exe": "edge", "chrome.exe": "chrome", "firefox.exe": "firefox"}
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, app_paths_root) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    sub = winreg.EnumKey(key, i)
                    if sub.lower() in app_map:
                        try:
                            with winreg.OpenKey(key, sub) as sk:
                                exe, _ = winreg.QueryValueEx(sk, "")
                        except OSError:
                            continue
                        if exe and os.path.isfile(exe):
                            return app_map[sub.lower()]
        except OSError:
            pass
    # 2.2 卸载项：用 DisplayName 匹配（无论装在哪）
    uninstall_roots = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for root in uninstall_roots:
            try:
                with winreg.OpenKey(hive, root) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        sub = winreg.EnumKey(key, i)
                        try:
                            with winreg.OpenKey(key, sub) as sk:
                                name, _ = winreg.QueryValueEx(sk, "DisplayName")
                        except OSError:
                            continue
                        low = name.lower()
                        for browser, kws in keywords.items():
                            if any(k in low for k in kws):
                                return browser
            except OSError:
                pass
    return None


def _detect_browser_mac():
    known = {
        "edge": ["msedge",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"],
        "chrome": ["google-chrome", "chrome", "chromium",
                   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"],
        "firefox": ["firefox",
                    "/Applications/Firefox.app/Contents/MacOS/firefox"],
    }
    for browser, cands in known.items():
        if any(_candidate_hit(c) for c in cands):
            return browser
    return None


def _detect_browser_linux():
    # 1) PATH 命令名 + 常见默认路径
    path_names = {
        "edge": ["microsoft-edge", "microsoft-edge-stable"],
        "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
        "firefox": ["firefox"],
    }
    known = {
        "edge": ["/usr/bin/microsoft-edge", "/opt/microsoft/msedge/msedge"],
        "chrome": ["/usr/bin/google-chrome"],
        "firefox": ["/usr/bin/firefox"],
    }
    for browser in ("edge", "chrome", "firefox"):
        if any(_candidate_hit(n) for n in path_names[browser]):
            return browser
        if any(os.path.isfile(p) for p in known[browser]):
            return browser
    # 2) whereis 探测非默认/非 PATH 安装（如 /opt、/usr/local 下）
    whereis_map = {
        "edge": ["microsoft-edge", "msedge", "edge"],
        "chrome": ["google-chrome", "chromium", "chrome"],
        "firefox": ["firefox"],
    }
    for browser, cmds in whereis_map.items():
        for cmd in cmds:
            try:
                out = subprocess.run(
                    ["whereis", "-b", cmd],
                    capture_output=True, text=True, timeout=10
                ).stdout
            except Exception:
                continue
            if ":" not in out:
                continue
            for path in out.split(":", 1)[1].split():
                if os.path.isfile(path):
                    return browser
    return None


def build_login_view(page, state, on_success):
    """构建登录页。

    :param state: 跨页面共享的 AppState（含 exporter）
    :param on_success: 登录成功后切换到的回调（无参数）
    """
    user_field = ft.TextField(
        label="有道云笔记邮箱账号",
        hint_text="例如 xxx@163.com",
        width=380,
    )
    # 输入框占位提示(hint_text)在 Flet 里不换行、超宽会被直接截断，
    # 所以把完整约束放到下方说明文字里（Text 可自动换行，保证完整显示）。
    account_tip = ft.Text(
        "只能填写邮箱地址（例如 xxx@163.com），不支持手机号登录。",
        size=12,
        color=ft.Colors.GREY,
        width=380,
    )
    status = ft.Text("", selectable=True, size=13)
    progress = ft.ProgressRing(visible=False, width=20, height=20)

    def safe_update():
        # 后台线程里调用 page.update() 偶尔会抛异常，绝不能让它中断登录流程
        try:
            page.update()
        except Exception:
            pass

    def set_status(msg, color=None):
        status.value = msg
        status.color = color
        safe_update()

    # ------------------------------------------------------------------
    # Selenium 兜底：让用户完成一次网页登录并抓取授权 cookie
    # ------------------------------------------------------------------
    def capture_cookies_via_selenium(status_fn):
        """打开浏览器，等待用户登录并抓取凭据。

        返回 ``(cookies, p_info_account)``：
        - ``cookies``：``[[name, value, domain, path], ...]``，仅含必需的
          YNOTE_CSTK / YNOTE_LOGIN / YNOTE_SESS；
        - ``p_info_account``：从浏览器 ``P_INFO`` cookie 第一项解析出的真实账号
          （形如 ``xxx@xx.com|1699999999|1`` 的第一段），解析不到则为空串。

        浏览器类型由 ``_detect_browser()`` 自动探测（Edge / Chrome / Firefox），
        对应驱动由 Selenium Manager 在首次运行时联网下载并缓存，无需手动配置；
        下载慢时可走 npmmirror 镜像（默认开启，见 ``_resolve_driver_path``）。
        """
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.firefox.options import Options as FirefoxOptions
        from selenium.common.exceptions import WebDriverException
        from selenium.webdriver.edge.service import Service as EdgeService
        from selenium.webdriver.chrome.service import Service as ChromeService
        from selenium.webdriver.firefox.service import Service as FirefoxService

        browser = _detect_browser()
        # 调试日志：仅在 flet run 开发模式下打印，打包后的 exe 不输出
        if not getattr(sys, "frozen", False):
            print("[detect] 探测到的浏览器 = {}".format(browser))
        if browser is None:
            raise RuntimeError(
                "未检测到已安装的浏览器，请安装 Microsoft Edge / Google Chrome / "
                "Firefox，或改用「从 cookies.json 导入」"
            )
        browser_name = {"edge": "Edge", "chrome": "Chrome", "firefox": "Firefox"}[browser]
        # 简洁提示：只告诉用户在做什么、大概需要多久；下载详情仅在调试日志中输出
        if _is_driver_cached(browser):
            status_fn("正在启动{}浏览器…".format(browser_name))
        else:
            status_fn(
                "正在下载{}浏览器驱动（约需 10~60 秒，仅首次需要），请稍候…".format(browser_name)
            )
        print("[selenium] 正在启动 {} 浏览器…".format(browser))

        # 调试模式：把 Selenium Manager 的全部日志（是否走镜像、实际下载地址等）打到控制台，
        # 但绝不回显到界面——界面只显示上面的简洁提示。
        # 将日志器级别设为 DEBUG 还会让 Selenium Manager 二进制附带 --debug，输出更详细。
        import logging

        sm_logger = None
        sm_handler = None
        prev_level = None
        if not getattr(sys, "frozen", False):
            class _SMDebugHandler(logging.Handler):
                def emit(self, record):
                    try:
                        print("[selenium-manager] " + record.getMessage())
                    except Exception:
                        pass

            sm_logger = logging.getLogger("selenium.webdriver.common.selenium_manager")
            sm_handler = _SMDebugHandler()
            prev_level = sm_logger.level
            sm_logger.setLevel(logging.DEBUG)
            sm_logger.addHandler(sm_handler)
        try:
            # 解析驱动路径（镜像加速 / 缓存复用）；首次运行会在此下载，可能耗时
            driver_path = _resolve_driver_path(browser)
            if not getattr(sys, "frozen", False):
                print("[selenium] 实际驱动路径 =", driver_path)
            try:
                if browser == "edge":
                    opts = EdgeOptions()  # 必须用 Edge 专用 Options，否则 browserName=chrome 会误下 chromedriver
                    # detach=True：防止会话建立后立即关闭浏览器，让用户完成登录
                    opts.add_experimental_option("detach", True)
                    if not getattr(sys, "frozen", False):
                        print("[selenium] 创建 Edge driver，options.browserName =",
                              opts.capabilities.get("browserName"))
                    driver = webdriver.Edge(
                        service=EdgeService(executable_path=driver_path), options=opts)
                elif browser == "chrome":
                    opts = ChromeOptions()
                    opts.add_experimental_option("detach", True)
                    if not getattr(sys, "frozen", False):
                        print("[selenium] 创建 Chrome driver，options.browserName =",
                              opts.capabilities.get("browserName"))
                    driver = webdriver.Chrome(
                        service=ChromeService(executable_path=driver_path), options=opts)
                else:  # firefox
                    driver = webdriver.Firefox(
                        service=FirefoxService(executable_path=driver_path), options=FirefoxOptions())
            except WebDriverException as ex:
                raise RuntimeError(
                    "无法启动{}（首次运行需联网下载对应驱动，请确认网络畅通）：{}".format(browser_name, ex)
                )
        finally:
            if sm_logger is not None:
                sm_logger.removeHandler(sm_handler)
                sm_logger.setLevel(prev_level)
        driver.set_page_load_timeout(60)
        try:
            # 打开登录页；首屏加载失败则重试，避免直接关闭浏览器
            for attempt in range(3):
                try:
                    driver.get(YOUDAO_WEB_URL)
                    break
                except WebDriverException as ex:
                    if attempt == 2:
                        raise RuntimeError("无法打开有道云笔记页面：{}".format(ex))
                    time.sleep(2)

            status_fn("{} 已打开，请在有道云笔记页面完成登录…".format(browser_name))
            print("[selenium] 浏览器已打开，等待用户登录…")
            deadline = time.time() + 360  # 最多等待 6 分钟
            while time.time() < deadline:
                # 浏览器被用户手动关闭则及时报错，避免无意义的空等
                try:
                    _ = driver.current_window_handle
                except WebDriverException:
                    raise RuntimeError("{} 窗口已被关闭，请重新点击登录".format(browser_name))
                try:
                    names = {c["name"] for c in driver.get_cookies()}
                except WebDriverException:
                    names = set()
                if set(REQUIRED_COOKIE_NAMES).issubset(names):
                    break
                time.sleep(2)
            else:
                raise TimeoutError("等待登录超时（约 6 分钟），未检测到授权 Cookie")

            mapping = {c["name"]: c for c in driver.get_cookies()}
            cookies = []
            for name in REQUIRED_COOKIE_NAMES:
                c = mapping[name]
                cookies.append(
                    [c["name"], c["value"], c.get("domain", ""), c.get("path", "/")]
                )
            # P_INFO 的第一项就是真实账号（形如 "xxx@xx.com|1699999999|1"）
            p_info_account = ""
            p_info = mapping.get("P_INFO")
            if p_info:
                try:
                    from urllib.parse import unquote

                    p_info_account = unquote(p_info.get("value", "")).split("|")[0].strip()
                except Exception:
                    p_info_account = ""
            print("[selenium] 已捕获授权 Cookie：", [c[0] for c in cookies])
            print("[selenium] P_INFO 解析出的账号：", repr(p_info_account))
            return cookies, p_info_account
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 登录主流程（在后台线程执行，避免界面卡死）
    # ------------------------------------------------------------------
    def get_real_account(exporter):
        """读取当前 cookies 真正对应的有道云笔记账号（邮箱）。

        取不到时返回空串；调用方据此决定是回退到用户输入还是直接失败。
        """
        try:
            return (exporter.api.get_real_account() or "").strip()
        except Exception:
            return ""

    def login_worker(user_name):
        print("[login] worker start, user =", repr(user_name))
        exporter = state.exporter
        need_web_login = False
        try:
            saved = db.get_cookies(user_name)
        except Exception as ex:
            # 读库异常不应阻断登录：当作首次登录，走 Selenium 兜底
            saved = None
            set_status("读取本地账户失败（{}），将重新登录".format(ex), ft.Colors.ORANGE)
        print("[login] db lookup result:", "hit" if saved else "miss")

        if saved:
            err = exporter.login(saved)
            if not err:
                _, verr = exporter.get_root_id()
                if not verr:
                    finish_login(user_name, "已使用保存的 Cookie 登录成功")
                    return
                err = verr
            set_status(
                "保存的 Cookie 已失效（{}），需要重新登录".format(err),
                ft.Colors.ORANGE,
            )
            need_web_login = True
        else:
            set_status(
                "首次登录，请在弹出的浏览器中完成有道云笔记网页登录",
                ft.Colors.BLUE,
            )
            need_web_login = True

        if need_web_login:
            print("[login] 进入 Selenium 兜底流程")
            try:
                new_cookies, p_info_account = capture_cookies_via_selenium(set_status)
            except Exception as ex:
                progress.visible = False
                set_status("网页登录失败：{}".format(ex), ft.Colors.RED)
                return

            err = exporter.login(new_cookies)
            if err:
                progress.visible = False
                set_status("登录失败：{}".format(err), ft.Colors.RED)
                return
            _, verr = exporter.get_root_id()
            if verr:
                progress.visible = False
                set_status("获取根目录失败：{}".format(verr), ft.Colors.RED)
                return
            # 规则 1：首次网页登录，按 Edge 中实际登录的邮箱账号存储 cookies
            # 优先取 /yws/api/self 接口返回的真实邮箱，P_INFO 仅作兜底
            real_account = get_real_account(exporter) or (p_info_account or "").strip()
            final_name = real_account or user_name
            try:
                db.save_cookies(final_name, new_cookies)
            except Exception as ex:
                progress.visible = False
                set_status("保存账户失败：{}".format(ex), ft.Colors.RED)
                return
            # 回填真实账号，方便后续登录直接复用
            user_field.value = final_name
            finish_login(
                final_name,
                "网页登录成功，已按账号「{}」保存 Cookie 到本地".format(final_name),
            )

    def finish_login(user_name, msg):
        state.user_name = user_name
        progress.visible = False
        set_status(msg, ft.Colors.GREEN)
        on_success()

    def on_login_click(e):
        user_name = (user_field.value or "").strip()
        if not user_name:
            set_status("请先输入有道云笔记邮箱账号", ft.Colors.RED)
            return
        if not EMAIL_RE.match(user_name):
            set_status(
                "邮箱账号格式不正确，请填写完整邮箱，例如 xxx@163.com",
                ft.Colors.RED,
            )
            return
        state.user_name = user_name
        progress.visible = True
        set_status("正在登录…")
        # 用 lambda 包裹，规避 run_thread 的参数传递差异
        page.run_thread(lambda: login_worker(user_name))

    def _cookies_from_session(exporter):
        """从已登录的 session 中取出三要素 cookie，重组成可存储的列表。

        导入 cookies.json 后，cookie 已装载进 ``exporter.api.session``，
        这里直接把它还原成 ``[[name, value, domain, path], ...]``，
        以便按用户名写入 accounts.db，保持与 Selenium 兜底路径一致的存储格式。
        """
        jar = exporter.api.session.cookies
        by_name = {c.name: c for c in jar}
        cookies = []
        for name in REQUIRED_COOKIE_NAMES:
            c = by_name.get(name)
            if c is None:
                raise ValueError("会话中缺少 Cookie：{}".format(name))
            cookies.append(
                [
                    c.name,
                    c.value,
                    getattr(c, "domain", "") or "",
                    getattr(c, "path", "/") or "/",
                ]
            )
        return cookies

    # 用于「从 cookies.json 导入」选择文件的文件选择器（Flet 1.0 服务）
    json_picker = ft.FilePicker()
    page.services.append(json_picker)

    # 复用命令行已有的 cookies.json 作为登录凭据，并把账户写入数据库
    async def on_import_file(e):
        # 导入前必须填写有道云笔记邮箱账号，否则不执行导入
        account = (user_field.value or "").strip()
        if not account:
            set_status("请先输入有道云笔记邮箱账号，再导入 cookies.json", ft.Colors.RED)
            return
        if not EMAIL_RE.match(account):
            set_status(
                "邮箱账号格式不正确，请填写完整邮箱，例如 xxx@163.com",
                ft.Colors.RED,
            )
            return
        # Flet 1.0 中 pick_files 是 async 方法，须 await；返回选中的文件列表
        files = await json_picker.pick_files(
            dialog_title="选择 cookies.json 文件",
            allowed_extensions=["json"],
            file_type=ft.FilePickerFileType.CUSTOM,
            allow_multiple=False,
        )
        if not files:
            set_status("未选择文件，已取消导入", ft.Colors.GREY)
            return
        path = files[0].path
        if not path:
            set_status("无法获取所选文件路径，请重试", ft.Colors.RED)
            return
        err = state.exporter.login_from_file(path)
        if err:
            set_status("从 cookies.json 导入失败：{}".format(err), ft.Colors.RED)
            return
        _, verr = state.exporter.get_root_id()
        if verr:
            set_status("cookies.json 已失效：{}".format(verr), ft.Colors.RED)
            return
        # 规则 3：导入时校验 cookies 对应的实际账号与用户输入是否一致
        real_account = get_real_account(state.exporter)
        if not real_account:
            set_status(
                "无法确认该 cookies 对应的有道云笔记账号，导入失败",
                ft.Colors.RED,
            )
            return
        if real_account.lower() != account.lower():
            set_status(
                "导入失败：你输入的账号「{}」与该 cookies 对应的账号「{}」不一致".format(
                    account, real_account
                ),
                ft.Colors.RED,
            )
            return
        try:
            cookies = _cookies_from_session(state.exporter)
            db.save_cookies(account, cookies)
        except Exception as ex:
            set_status("保存账户失败：{}".format(ex), ft.Colors.RED)
            return
        finish_login(
            account,
            "已从 {} 导入，账号「{}」校验通过并保存到本地".format(
                os.path.basename(path), account
            ),
        )

    view = ft.Column(
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        spacing=16,
        controls=[
            ft.Container(height=40),
            ft.Text("有道云笔记导出", size=26, weight=ft.FontWeight.BOLD),
            ft.Text("YoudaoNoteExporter", size=13, color=ft.Colors.GREY),
            ft.Text("登录以开始导出", size=14, color=ft.Colors.GREY),
            ft.Container(
                padding=24,
                border=ft.Border.all(1, ft.Colors.OUTLINE),
                border_radius=12,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=14,
                    controls=[
                        user_field,
                        account_tip,
                        ft.Row(
                            alignment=ft.MainAxisAlignment.CENTER,
                            controls=[
                                ft.FilledButton(
                                    "登录",
                                    icon=ft.Icons.LOGIN,
                                    width=180,
                                    on_click=on_login_click,
                                ),
                                progress,
                            ],
                        ),
                        ft.TextButton(
                            "或从 cookies.json 导入", on_click=on_import_file
                        ),
                    ],
                ),
            ),
            status,
        ],
    )
    return view
