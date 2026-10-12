# -*- coding: utf-8 -*-
"""列出有道云笔记的目录树 / 笔记清单（只读元信息，不下载笔记内容）。

依赖：cookies.json 已配置且未过期。不会读写 youdaonote/ 下的任何文件。

用法：
    python tools/list_notes.py                  # 打印整棵树
    python tools/list_notes.py 工作/2026        # 只打印指定子目录（支持多级，兼容 \\ 分隔）
    python tools/list_notes.py --flat           # 额外输出扁平清单（id/名称/创建/修改，制表符分隔）
    python tools/list_notes.py 工作 --flat      # 两个参数可组合，顺序任意
"""
import os
import sys
import time

# 本文件位于 <项目根>/tools/ 下，上溯一级即为项目根
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.api import YoudaoNoteApi  # noqa: E402


def ts_text(value):
    """把有道的时间戳转为可读文本。

    有道云笔记的 createTimeForSort / modifyTimeForSort 是秒级时间戳，不是毫秒。
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(value)))
    except (TypeError, ValueError):
        return ""


def get_root_id(api):
    """获取根目录 ID，并兼容「Cookie 失效时返回错误体而非异常」的情况。

    Cookie 过期时，根接口返回 HTTP 500 且响应体是
    {"scope":"SECURITY","error":"207","message":"...AUTHENTICATION_FAILURE..."}，
    没有 fileEntry 字段，直接取下标会 KeyError，这里转成可读提示。

    :param api: YoudaoNoteApi 实例
    :return: root_id, error_msg
    """
    info = api.get_root_dir_info_id()
    file_entry = info.get("fileEntry") if isinstance(info, dict) else None
    if not file_entry or not file_entry.get("id"):
        return "", (
            "获取根目录失败，接口返回：{}\n"
            "常见原因是「cookies.json」已过期：请重新登录 https://note.youdao.com 后更新 Cookie"
        ).format(info)
    return file_entry["id"], ""


def resolve_dir_id(api, path):
    """把 "工作/2026/项目A" 这样的路径解析为目录 ID。

    空路径表示根目录（导出全部笔记）。

    :param api: YoudaoNoteApi 实例
    :param path: config.json 里 ydnote_dir 风格的路径
    :return: dir_id, error_msg
    """
    root_id, error_msg = get_root_id(api)
    if error_msg:
        return "", error_msg
    segments = [seg.strip() for seg in (path or "").replace("\\", "/").split("/") if seg.strip()]

    dir_id = root_id
    for name in segments:
        info = api.get_dir_info_by_id(dir_id)
        next_id = ""
        for entry in info.get("entries") or []:
            file_entry = entry.get("fileEntry") or {}
            if file_entry.get("name") == name:
                next_id = file_entry.get("id", "")
                break
        if not next_id:
            return "", "目录「{}」未找到，请检查路径是否写错（当前层级：{}）".format(name, path)
        dir_id = next_id
    return dir_id, ""


def walk(api, dir_id, depth=0, flat=None):
    """递归遍历目录，打印树形结构。

    :param api: YoudaoNoteApi 实例
    :param dir_id: 当前目录 ID
    :param depth: 当前层级，用于缩进
    :param flat: 传入 list 时收集 (id, 名称, 创建时间, 修改时间)
    :return: (笔记数, 文件夹数)
    """
    info = api.get_dir_info_by_id(dir_id)
    if "entries" not in info:
        # Cookie 失效或接口变更时，有道返回的不是报错码而是缺少 entries 的响应体，
        # 这里显式区分，避免和「空文件夹」混淆
        raise KeyError(
            "接口返回中没有 entries 字段，可能是 Cookie 已过期或接口已变更：{}".format(info)
        )

    note_count = 0
    dir_count = 0
    for entry in info.get("entries") or []:
        file_entry = entry.get("fileEntry") or {}
        name = file_entry.get("name", "")
        entry_id = file_entry.get("id", "")
        if file_entry.get("dir"):
            dir_count += 1
            print("  " * depth + "[D] " + name)
            sub_notes, sub_dirs = walk(api, entry_id, depth + 1, flat)
            note_count += sub_notes
            dir_count += sub_dirs
        else:
            note_count += 1
            print(
                "  " * depth
                + "[F] {}  （修改 {}）".format(name, ts_text(file_entry.get("modifyTimeForSort")))
            )
            if flat is not None:
                flat.append(
                    (
                        entry_id,
                        name,
                        ts_text(file_entry.get("createTimeForSort")),
                        ts_text(file_entry.get("modifyTimeForSort")),
                    )
                )
    return note_count, dir_count


def main(argv=None):
    """命令行入口，返回退出码"""
    argv = list(sys.argv[1:] if argv is None else argv)

    flat = [] if "--flat" in argv else None
    args = [item for item in argv if item != "--flat"]

    api = YoudaoNoteApi()
    error_msg = api.login_by_cookies()
    if error_msg:
        print("登录失败：{}".format(error_msg))
        return 1

    dir_id, error_msg = resolve_dir_id(api, args[0] if args else "")
    if error_msg:
        print(error_msg)
        return 1

    try:
        note_count, dir_count = walk(api, dir_id, flat=flat)
    except KeyError as error:
        print("遍历失败：{}".format(format(error)))
        return 1

    print("\n共 {} 篇笔记，{} 个文件夹".format(note_count, dir_count))

    if flat is not None:
        print("\n--- 扁平清单（id\t名称\t创建时间\t修改时间）---")
        for row in flat:
            print("\t".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
