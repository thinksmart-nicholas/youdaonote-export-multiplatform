import os
import platform
import sys
import tempfile

APP_NAME = "youdaonote-export-multiplatform"
#: 用于覆盖数据目录的环境变量，便于测试与便携部署
ENV_DATA_DIR = "YOUDAONOTE_DATA_DIR"


def get_script_directory():
    """获取脚本（项目）根目录，即**代码资源目录**

    - 打包成 exe 时返回可执行文件所在目录
    - 普通脚本时返回 core/ 的上级目录（项目根目录）的**绝对路径**

    返回绝对路径是为了保证 config.json / cookies.json / logs/ 的定位
    不受「从哪里启动脚本」影响：早期实现返回 "."，从其它目录运行脚本时
    会因找不到配置文件而失败。

    注意：打包后（Flet / PyInstaller）该目录**可能是只读的**，
    因此不要把运行时产生的数据写到这里，请使用 `resolve_data_dir()`。
    """

    if getattr(sys, "frozen", False):
        # 如果是打包后的可执行文件
        return os.path.dirname(sys.executable)
    # core/common.py -> core -> 项目根目录
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_user_data_dir():
    """获取跨平台的「用户数据目录」（可写），用于存放配置 / Cookie / 日志

    - Windows: ``%APPDATA%\\youdaonote-export-multiplatform``
    - macOS:   ``~/Library/Application Support/youdaonote-export-multiplatform``
    - Linux:   ``$XDG_DATA_HOME`` 或 ``~/.local/share/youdaonote-export-multiplatform``

    可用环境变量 ``YOUDAONOTE_DATA_DIR`` 覆盖，方便测试与便携部署。
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return override

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif system == "Darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, APP_NAME)


def _is_writable_dir(path):
    """判断目录是否可写（不存在时先尝试创建，再落一个临时文件验证）"""
    try:
        os.makedirs(path, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=path)
        os.close(fd)
        os.remove(tmp_path)
        return True
    except OSError:
        return False


def _candidate_dirs():
    """按优先级返回数据目录候选：项目根优先（兼容既有文件），用户目录兜底"""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return [override]
    return [get_script_directory(), get_user_data_dir()]


def resolve_data_dir():
    """挑一个真正可写的数据目录

    优先使用项目根目录（兼容既有部署）；当项目根不可写时（Flet / PyInstaller
    打包后应用目录只读）自动回退到用户数据目录。
    """
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        os.makedirs(override, exist_ok=True)
        return override

    script_dir = get_script_directory()
    if _is_writable_dir(script_dir):
        return script_dir

    user_dir = get_user_data_dir()
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


def resolve_data_file(filename):
    """定位一个数据文件（config.json / cookies.json 等）

    读取场景：按顺序返回第一个**已存在**的路径，保证老用户放在项目根的
    配置依然生效；都不存在时返回可写目录下的目标路径，供首次写入使用。
    """
    for directory in _candidate_dirs():
        candidate = os.path.join(directory, filename)
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(resolve_data_dir(), filename)
