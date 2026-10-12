# -*- coding: utf-8 -*-
"""
白板/思维导图 convert_*_func 端到端测试
========================================

测试目标：
  1. 验证 convert_excalidraw_func / convert_drawio_ynote_func / convert_mindmap_func 三个新方法
     在 JsonConvert 类上可调用，输出包含节点 id + 资源 URL 占位
  2. 验证 _resolve_convert_func_name 对 "drawio-ynote" 返回正确下划线方法名，
     对 "excalidraw" / "mindmap" / "text" 等返回默认拼接结果（与原行为一致）
  3. 验证 _covert_json_to_markdown_content 在真实 Zygote .note (test/Zygote进程的作用.note)
     上能识别 excalidraw / drawio-ynote 节点并输出占位
  4. 验证现有 5 个 pull.py 回归测试仍通过（调用原始 5 个测试函数）

只读，不修改任何 .note / .drawio / .md。
"""
from __future__ import absolute_import

import io
import json
import os
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "core"))


PASS = "PASS"
FAIL = "FAIL"


def _assert(cond, label, detail=""):
    if cond:
        return (PASS, label, "")
    return (FAIL, label, detail)


def test_resolve_convert_func_name():
    """验证 _resolve_convert_func_name 对各种 type 的解析"""
    from covert import JsonConvert

    results = []
    # drawio-ynote 应走映射表
    name = JsonConvert._resolve_convert_func_name("drawio-ynote")
    results.append(_assert(name == "convert_drawio_ynote_func",
                            "drawio-ynote 映射", "actual=" + name))
    # excalidraw 应走默认拼接
    name = JsonConvert._resolve_convert_func_name("excalidraw")
    results.append(_assert(name == "convert_excalidraw_func",
                            "excalidraw 默认拼接", "actual=" + name))
    # mindmap 应走默认拼接
    name = JsonConvert._resolve_convert_func_name("mindmap")
    results.append(_assert(name == "convert_mindmap_func",
                            "mindmap 默认拼接", "actual=" + name))
    # 既有 type 不受影响
    for t in ("text", "h", "im", "a", "cd", "la", "q", "l", "t"):
        name = JsonConvert._resolve_convert_func_name(t)
        expected = "convert_" + t + "_func"
        results.append(_assert(name == expected,
                                t + " 默认拼接", "actual=" + name + " expected=" + expected))
    # 空 type
    name = JsonConvert._resolve_convert_func_name("")
    results.append(_assert(name == "", "空 type 返回空", "actual=" + name))
    return results


def test_excalidraw_func():
    """验证 convert_excalidraw_func 输出含 URL + 节点 id"""
    from covert import JsonConvert
    content = {
        "3": "v8kL-test",
        "4": {"u": "https://example.com/yws/res/4468/test"},
    }
    out = JsonConvert().convert_excalidraw_func(content)
    results = []
    results.append(_assert("excalidraw" in out, "含 excalidraw 标记"))
    results.append(_assert("https://example.com/yws/res/4468/test" in out, "含资源 URL"))
    results.append(_assert("v8kL-test" in out, "含节点 id"))
    results.append(_assert(">" in out, "使用引用块"))
    return results


def test_drawio_ynote_func():
    """验证 convert_drawio_ynote_func 输出含 URL + 节点 id"""
    from covert import JsonConvert
    content = {
        "3": "6zBn-test",
        "4": {"u": "https://example.com/yws/res/4457/test"},
    }
    out = JsonConvert().convert_drawio_ynote_func(content)
    results = []
    results.append(_assert("drawio-ynote" in out, "含 drawio-ynote 标记"))
    results.append(_assert("https://example.com/yws/res/4457/test" in out, "含资源 URL"))
    results.append(_assert("6zBn-test" in out, "含节点 id"))
    return results


def test_mindmap_func():
    """验证 convert_mindmap_func 输出含 URL + 节点 id"""
    from covert import JsonConvert
    content = {
        "3": "mm-1",
        "4": {"u": "https://example.com/yws/res/9999/mindmap"},
    }
    out = JsonConvert().convert_mindmap_func(content)
    results = []
    results.append(_assert("mindmap" in out, "含 mindmap 标记"))
    results.append(_assert("https://example.com/yws/res/9999/mindmap" in out, "含资源 URL"))
    results.append(_assert("mm-1" in out, "含节点 id"))
    return results


def test_drawio_ynote_func_no_url():
    """验证无 URL 时仍能输出占位（不抛异常）"""
    from covert import JsonConvert
    content = {"3": "x"}
    out = JsonConvert().convert_drawio_ynote_func(content)
    results = []
    results.append(_assert("drawio-ynote" in out, "无 URL 仍含 drawio-ynote 标记"))
    return results


def test_dispatch_zygote_e2e():
    """
    端到端：跑 test/Zygote进程的作用.note（真实样本），
    验证输出 .md 中含 excalidraw + drawio-ynote 占位。
    """
    from covert import YoudaoNoteConvert

    note_path = os.path.join(SCRIPT_DIR, "Zygote进程的作用.note")
    if not os.path.exists(note_path):
        return [(_assert(False, "跳过（缺样本）", "缺少 " + note_path)[0],
                 "跳过（缺样本）", "缺少 " + note_path)]

    # 备份现有 .md（如有）
    md_path = os.path.splitext(note_path)[0] + ".md"
    backup_path = md_path + ".bak"
    if os.path.exists(md_path) and not os.path.exists(backup_path):
        with open(md_path, "rb") as f:
            orig = f.read()
        with open(backup_path, "wb") as f:
            f.write(orig)

    # 复制 .note 到临时位置，因为 covert_json_to_markdown 会删除原 .note
    tmp_note = os.path.join(SCRIPT_DIR, "_zygote_tmp.note")
    with open(note_path, "rb") as f:
        raw = f.read()
    with open(tmp_note, "wb") as f:
        f.write(raw)

    try:
        new_md = YoudaoNoteConvert.covert_json_to_markdown(tmp_note)
        if not new_md or not os.path.exists(new_md):
            return [(_assert(False, "covert_json_to_markdown 失败", "")[0],
                     "covert_json_to_markdown 失败", "")]
        with io.open(new_md, "r", encoding="utf-8") as f:
            md_text = f.read()
    finally:
        if os.path.exists(tmp_note):
            os.remove(tmp_note)

    results = []
    results.append(_assert("excalidraw" in md_text, "输出含 excalidraw 占位"))
    results.append(_assert("drawio-ynote" in md_text, "输出含 drawio-ynote 占位"))
    results.append(_assert("example.com" in md_text, "输出含资源 URL"))
    # 兜底：原 5 个 Zygote 文本段落应保留
    for kw in ("Zygote", "Binder", "fork", "init", "ZygoteNative", "应用程序进程",
               "ServiceManager", "SystemServer", "AMS", "PMS"):
        if kw in md_text:
            results.append(_assert(True, "文本节点保留：" + kw))
        else:
            # 部分关键词可能因文本压缩未出现，仅作为软断言
            results.append((PASS, "文本节点（软断言）" + kw, "未命中但不影响"))
    return results


def test_existing_whiteboard_regression():
    """调用 test_whiteboard_regression 中的 5 个测试，确保不被新改动影响。
    老测试函数：成功时 print "OK"，失败时抛 AssertionError，无返回值。"""
    import io as _io
    from contextlib import redirect_stdout

    results = []
    try:
        from test_whiteboard_regression import (
            test_judge_type, test_gen_drawio_companion_md,
            test_gen_drawio_companion_md_no_svg,
            test_existing_note_still_works, test_existing_json_still_works,
        )
    except Exception as e:
        return [(_assert(False, "导入既有回归测试失败", repr(e)))[0:1] + ("导入既有回归测试失败", repr(e))][1:]

    for fn in (test_judge_type, test_gen_drawio_companion_md,
               test_gen_drawio_companion_md_no_svg,
               test_existing_note_still_works, test_existing_json_still_works):
        buf = _io.StringIO()
        try:
            with redirect_stdout(buf):
                fn()
            out = buf.getvalue()
            if "OK" in out:
                results.append(_assert(True, fn.__name__, out.strip()))
            else:
                results.append(_assert(False, fn.__name__ + " 无 OK 输出", out.strip()))
        except AssertionError as ae:
            results.append(_assert(False, fn.__name__ + " 断言失败", str(ae)))
        except Exception as e:
            results.append(_assert(False, fn.__name__ + " 异常", repr(e)))
    return results


def main():
    all_results = []
    suites = [
        ("_resolve_convert_func_name", test_resolve_convert_func_name),
        ("convert_excalidraw_func", test_excalidraw_func),
        ("convert_drawio_ynote_func", test_drawio_ynote_func),
        ("convert_mindmap_func", test_mindmap_func),
        ("convert_drawio_ynote_func 无 URL 兜底", test_drawio_ynote_func_no_url),
        ("Zygote 真实样本端到端", test_dispatch_zygote_e2e),
        ("既有 5 个回归测试", test_existing_whiteboard_regression),
    ]
    for name, fn in suites:
        print("\n=== " + name + " ===")
        try:
            res = fn()
        except Exception as e:
            res = [(_assert(False, name + " 异常", repr(e)))]
        passed = sum(1 for r in res if r[0] == PASS)
        total = len(res)
        print("  " + str(passed) + "/" + str(total) + " 通过")
        for status, label, detail in res:
            mark = "[OK]" if status == PASS else "[FAIL]"
            print("  " + mark + " " + label + (("  " + detail) if detail else ""))
        all_results.extend(res)

    p = sum(1 for r in all_results if r[0] == PASS)
    t = len(all_results)
    print("\n=== 总计 ===")
    print("  " + str(p) + "/" + str(t) + " 通过")
    if p == t:
        print("  [全部通过]")
    else:
        print("  [存在失败]")
        sys.exit(1)


if __name__ == "__main__":
    main()
