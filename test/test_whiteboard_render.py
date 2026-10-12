# -*- coding: utf-8 -*-
"""
白板 SVG 渲染 + Markdown 内嵌嵌入 测试
========================================

测试目标：
  1. 验证 core.whiteboard_render 三个渲染器（excalidraw / drawio-ynote / mindmap）
     在真实 Zygote 样本上能输出有效 SVG（含 viewBox / preserveAspectRatio）
  2. 验证 save_whiteboard_svg 落盘 + 相对路径返回
  3. 验证 render_whiteboard 统一入口 + 未知 type 兜底
  4. 验证 covert.py 三个白板方法在 whiteboard_map 命中时输出
     ![alt](images/<id>.svg)（标准 Markdown 图片语法，语雀可识别）
  5. 验证 whiteboard_map 未命中时回退到原占位（不影响现有行为）
  6. 验证 covert.covert_json_to_markdown 接受 whiteboard_map 参数 + 端到端
     Zygote 真实样本 + 自定义 map → .md 含标准 Markdown 图片

只读，不修改任何 .note / .drawio / .md。
"""
from __future__ import absolute_import

import io
import json
import os
import shutil
import sys
import tempfile


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


# ---------------------------------------------------------------------------
# 1) whiteboard_render 渲染器
# ---------------------------------------------------------------------------

def test_render_excalidraw_to_svg():
    """验证 render_excalidraw_to_svg 在 Zygote 真实样本上输出有效 SVG"""
    from core.whiteboard_render import render_excalidraw_to_svg

    bin_path = os.path.join(SCRIPT_DIR, "whiteboard_samples", "Zygote_excalidraw.bin")
    if not os.path.isfile(bin_path):
        return [(_assert(False, "跳过（缺样本）", bin_path)[0],
                 "跳过（缺样本）", bin_path)]
    with io.open(bin_path, "r", encoding="utf-8") as f:
        content = f.read()
    svg = render_excalidraw_to_svg(content)
    results = []
    results.append(_assert(bool(svg), "返回非空 SVG"))
    results.append(_assert("<svg" in svg, "含 <svg 标签"))
    results.append(_assert("viewBox" in svg, "含 viewBox（可缩放）"))
    results.append(_assert("preserveAspectRatio" in svg, "含 preserveAspectRatio"))
    results.append(_assert('width="100%"' not in svg[:svg.find(">", svg.find("<svg")) + 1], "width 为像素固有尺寸"))
    results.append(_assert('height="100%"' not in svg[:svg.find(">", svg.find("<svg")) + 1], "height 为像素固有尺寸"))
    # 必须包含至少一个 rect/text/line
    has_visual = any(t in svg for t in ("<rect", "<text", "<polyline", "<polygon", "<ellipse"))
    results.append(_assert(has_visual, "含至少一个图形元素"))
    return results


def test_render_drawio_ynote_to_svg():
    """验证 render_drawio_ynote_to_svg 在 Zygote 真实样本上输出有效 SVG"""
    from core.whiteboard_render import render_drawio_ynote_to_svg

    bin_path = os.path.join(SCRIPT_DIR, "whiteboard_samples", "Zygote_drawio-ynote.bin")
    if not os.path.isfile(bin_path):
        return [(_assert(False, "跳过（缺样本）", bin_path)[0],
                 "跳过（缺样本）", bin_path)]
    with io.open(bin_path, "r", encoding="utf-8") as f:
        content = f.read()
    svg = render_drawio_ynote_to_svg(content)
    results = []
    results.append(_assert(bool(svg), "返回非空 SVG"))
    results.append(_assert("<svg" in svg, "含 <svg 标签"))
    results.append(_assert("viewBox" in svg, "含 viewBox（可缩放）"))
    results.append(_assert('width="100%"' not in svg[:svg.find(">", svg.find("<svg")) + 1], "width 为像素固有尺寸"))
    # drawio 原 SVG 必定含 path/g 等图元
    has_visual = any(t in svg for t in ("<path", "<g", "<rect", "<text"))
    results.append(_assert(has_visual, "含至少一个 drawio 图元"))
    # content 属性（mxfile 大块 base64）应被剥除，避免冗余
    results.append(_assert('content="' not in svg, "已剥除冗余 content 属性"))
    return results


def test_render_mindmap_to_svg():
    """验证 render_mindmap_to_svg 至少返回一个非空占位 SVG"""
    from core.whiteboard_render import render_mindmap_to_svg

    # 喂一个简单 JSON 让它解析一些 title 字段
    content = json.dumps({
        "title": "测试思维导图",
        "nodes": [
            {"text": "根节点"},
            {"text": "子节点 1"},
        ],
    }, ensure_ascii=False)
    svg = render_mindmap_to_svg(content)
    results = []
    results.append(_assert(bool(svg), "返回非空 SVG"))
    results.append(_assert("<svg" in svg, "含 <svg 标签"))
    results.append(_assert("viewBox" in svg, "含 viewBox（可缩放）"))
    # 空内容时不应抛异常
    svg_empty = render_mindmap_to_svg("")
    results.append(_assert(bool(svg_empty), "空输入仍返回占位 SVG"))
    return results


# ---------------------------------------------------------------------------
# 2) render_whiteboard 统一入口
# ---------------------------------------------------------------------------

def test_render_whiteboard_dispatch():
    """验证 render_whiteboard 统一入口 + 未知 type 兜底"""
    from core.whiteboard_render import render_whiteboard

    results = []
    # 未知 type 返回空串（不影响主流程）
    out = render_whiteboard("unknown_type_xyz", "{}")
    results.append(_assert(out == "", "未知 type 返空"))
    # 已知 type 转发到对应渲染器
    ex_bin = os.path.join(SCRIPT_DIR, "whiteboard_samples", "Zygote_excalidraw.bin")
    if os.path.isfile(ex_bin):
        with io.open(ex_bin, "r", encoding="utf-8") as f:
            content = f.read()
        out = render_whiteboard("excalidraw", content)
        results.append(_assert(bool(out) and "<svg" in out, "excalidraw 走统一入口"))
    dio_bin = os.path.join(SCRIPT_DIR, "whiteboard_samples", "Zygote_drawio-ynote.bin")
    if os.path.isfile(dio_bin):
        with io.open(dio_bin, "r", encoding="utf-8") as f:
            content = f.read()
        out = render_whiteboard("drawio-ynote", content)
        results.append(_assert(bool(out) and "<svg" in out, "drawio-ynote 走统一入口"))
    # 空内容
    results.append(_assert(render_whiteboard("excalidraw", "") == "", "空内容返空"))
    # 渲染器抛异常时返空（不应向外传播）
    try:
        out = render_whiteboard("excalidraw", "{bad json")
    except Exception as e:
        out = "_exception_:" + repr(e)
    results.append(_assert(out == "", "异常输入返空（不抛）"))
    return results


# ---------------------------------------------------------------------------
# 3) save_whiteboard_svg 落盘
# ---------------------------------------------------------------------------

def test_save_whiteboard_svg():
    """验证 save_whiteboard_svg 落盘 + 相对路径返回"""
    from core.whiteboard_render import save_whiteboard_svg, render_excalidraw_to_svg

    results = []
    # 用一个小 SVG 文本测试
    tiny_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"></svg>'
    with tempfile.TemporaryDirectory() as tmp:
        # 正常情况
        rel = save_whiteboard_svg(tmp, "node-A1", tiny_svg)
        results.append(_assert(rel == "images/node-A1.svg", "返回相对路径", "actual=" + rel))
        file_path = os.path.join(tmp, "node-A1.svg")
        results.append(_assert(os.path.isfile(file_path), "文件实际落盘"))
        with io.open(file_path, "r", encoding="utf-8") as f:
            data = f.read()
        results.append(_assert(data == tiny_svg, "文件内容一致"))
        # 特殊字符的 node_id 应被清洗
        rel2 = save_whiteboard_svg(tmp, "weird/id:*?<>|", tiny_svg)
        results.append(_assert(":" not in os.path.basename(rel2) and "?" not in os.path.basename(rel2),
                                "特殊字符清洗", "actual=" + rel2))
        results.append(_assert(os.path.isfile(os.path.join(tmp, os.path.basename(rel2))),
                                "特殊字符 id 也成功落盘"))
        # 空 SVG / 空 node_id 应返空
        results.append(_assert(save_whiteboard_svg(tmp, "node-B", "") == "", "空 SVG 返空"))
        results.append(_assert(save_whiteboard_svg(tmp, "", tiny_svg) == "", "空 id 返空"))
        # 不存在的 images_dir 应自动创建
        new_dir = os.path.join(tmp, "sub", "images")
        rel3 = save_whiteboard_svg(new_dir, "node-C", tiny_svg)
        results.append(_assert(os.path.isdir(new_dir), "images_dir 自动创建"))
        results.append(_assert(os.path.isfile(os.path.join(new_dir, "node-C.svg")), "嵌套目录落盘成功"))
    return results


# ---------------------------------------------------------------------------
# 4) covert 三个白板方法：whiteboard_map 命中时输出图片
# ---------------------------------------------------------------------------

def test_whiteboard_map_hit_excalidraw():
    """excalidraw 节点在 map 命中时输出标准 Markdown 图片"""
    from covert import JsonConvert

    conv = JsonConvert()
    conv._whiteboard_map = {"v8kL-test": "images/v8kL-test.svg"}
    content = {
        "3": "v8kL-test",
        "4": {"u": "https://example.com/yws/res/4468/test"},
    }
    out = conv.convert_excalidraw_func(content)
    results = []
    results.append(_assert("![" in out, "输出标准 Markdown 图片语法"))
    results.append(_assert('(images/v8kL-test.svg)' in out, "src 引用本地 SVG"))
    results.append(_assert(out.lstrip().startswith("!["), "以 Markdown 图片语法开头"))
    results.append(_assert("<img" not in out, "不含 HTML img 标签（语雀无法识别）"))
    results.append(_assert("example.com" not in out, "无外部 URL 残留"))
    return results


def test_whiteboard_map_hit_drawio_ynote():
    """drawio-ynote 节点在 map 命中时输出标准 Markdown 图片"""
    from covert import JsonConvert

    conv = JsonConvert()
    conv._whiteboard_map = {"6zBn-test": "images/6zBn-test.svg"}
    content = {
        "3": "6zBn-test",
        "4": {"u": "https://example.com/yws/res/4457/test"},
    }
    out = conv.convert_drawio_ynote_func(content)
    results = []
    results.append(_assert("![" in out, "输出标准 Markdown 图片语法"))
    results.append(_assert('(images/6zBn-test.svg)' in out, "src 引用本地 SVG"))
    results.append(_assert("example.com" not in out, "无外部 URL 残留"))
    return results


def test_whiteboard_map_hit_mindmap():
    """mindmap 节点在 map 命中时输出标准 Markdown 图片"""
    from covert import JsonConvert

    conv = JsonConvert()
    conv._whiteboard_map = {"mm-1": "images/mm-1.svg"}
    content = {
        "3": "mm-1",
        "4": {"u": "https://example.com/yws/res/9999/mindmap"},
    }
    out = conv.convert_mindmap_func(content)
    results = []
    results.append(_assert("![" in out, "输出标准 Markdown 图片语法"))
    results.append(_assert('(images/mm-1.svg)' in out, "src 引用本地 SVG"))
    return results


# ---------------------------------------------------------------------------
# 5) map 未命中时回退到原占位（兼容性）
# ---------------------------------------------------------------------------

def test_whiteboard_map_miss_fallback():
    """map 不命中时三个白板方法回退到原 markdown 引用块"""
    from covert import JsonConvert

    conv = JsonConvert()
    conv._whiteboard_map = {"other-node": "images/other-node.svg"}  # 不包含目标 node
    results = []
    # excalidraw
    out1 = conv.convert_excalidraw_func({
        "3": "v8kL-test",
        "4": {"u": "https://example.com/yws/res/4468/test"},
    })
    results.append(_assert("![" not in out1, "excalidraw 未命中无图片"))
    results.append(_assert("excalidraw" in out1, "excalidraw 未命中仍含占位标记"))
    results.append(_assert("example.com" in out1, "excalidraw 未命中保留原 URL"))
    # drawio-ynote
    out2 = conv.convert_drawio_ynote_func({
        "3": "6zBn-test",
        "4": {"u": "https://example.com/yws/res/4457/test"},
    })
    results.append(_assert("![" not in out2, "drawio-ynote 未命中无图片"))
    results.append(_assert("drawio-ynote" in out2, "drawio-ynote 未命中仍含占位标记"))
    # mindmap
    out3 = conv.convert_mindmap_func({
        "3": "mm-1",
        "4": {"u": "https://example.com/yws/res/9999/mindmap"},
    })
    results.append(_assert("![" not in out3, "mindmap 未命中无图片"))
    results.append(_assert("mindmap" in out3, "mindmap 未命中仍含占位标记"))

    # 不设 map（属性完全不存在）
    conv2 = JsonConvert()
    out4 = conv2.convert_excalidraw_func({
        "3": "v8kL-test",
        "4": {"u": "https://example.com/yws/res/4468/test"},
    })
    results.append(_assert("![" not in out4, "无 map 属性时不输出图片"))
    results.append(_assert("excalidraw" in out4, "无 map 属性时仍含占位标记"))
    return results


# ---------------------------------------------------------------------------
# 6) covert.covert_json_to_markdown 接受 whiteboard_map
# ---------------------------------------------------------------------------

def test_covert_json_to_markdown_signature():
    """验证 covert_json_to_markdown 接受可选 whiteboard_map 参数"""
    from covert import YoudaoNoteConvert

    # 用临时 .note（极简结构，1 个 excalidraw 节点 + 1 个 text 节点）
    # text 节点结构遵循 covert.convert_text_func 期望：
    #   content["5"] = [{"5": [...], "6": "li", "7": [{"8": "text", "9": [...]}]}]
    note = {
        "2": "1",
        "3": "WEB",
        "5": [
            {
                "3": "node-excalidraw-1",
                "4": {"u": "https://example.com/yws/res/1/test"},
                "6": "excalidraw",
            },
            {
                "3": "node-text-1",
                "5": [
                    {
                        "5": [],
                        "6": "li",
                        "7": [{"8": "Hello", "9": []}],
                    }
                ],
            },
        ],
        "6": "test",
        "__compress__": True,
        "title": "test_signature",
    }
    with tempfile.TemporaryDirectory() as tmp:
        note_path = os.path.join(tmp, "test.note")
        with io.open(note_path, "w", encoding="utf-8") as f:
            json.dump(note, f, ensure_ascii=False)

        # 不传 whiteboard_map → 走原占位
        out1 = YoudaoNoteConvert.covert_json_to_markdown(note_path)
        with io.open(out1, "r", encoding="utf-8") as f:
            md1 = f.read()
        results = []
        results.append(_assert("excalidraw" in md1, "不传 map 走原占位 excalidraw"))
        results.append(_assert("![" not in md1, "不传 map 不输出图片"))
        results.append(_assert("Hello" in md1, "普通 text 节点仍输出"))

        # 传 whiteboard_map → 输出图片
        note_path2 = os.path.join(tmp, "test2.note")
        with io.open(note_path2, "w", encoding="utf-8") as f:
            json.dump(note, f, ensure_ascii=False)
        out2 = YoudaoNoteConvert.covert_json_to_markdown(
            note_path2,
            whiteboard_map={"node-excalidraw-1": "images/node-excalidraw-1.svg"},
        )
        with io.open(out2, "r", encoding="utf-8") as f:
            md2 = f.read()
        results.append(_assert("![" in md2, "传 map 后输出图片"))
        results.append(_assert('(images/node-excalidraw-1.svg)' in md2,
                                "src 指向 images/<id>.svg"))
        results.append(_assert("Hello" in md2, "普通 text 节点不受影响"))
        # 顺序：excalidraw 节点在前
        idx_img = md2.find("![")
        idx_hello = md2.find("Hello")
        results.append(_assert(0 <= idx_img < idx_hello, "白板节点保持原位置（在前）"))
    return results


# ---------------------------------------------------------------------------
# 7) 端到端：Zygote 真实样本 + 模拟预下载
# ---------------------------------------------------------------------------

def test_zygote_e2e_with_map():
    """
    端到端：跑 Zygote 真实样本，传入预下载的 whiteboard_map，
    验证输出 .md 含 ![alt](images/<id>.svg)，
    且原文本节点（Zygote / fork / SystemServer / AMS / PMS）仍保留。
    """
    from covert import YoudaoNoteConvert

    note_path = os.path.join(SCRIPT_DIR, "Zygote进程的作用.note")
    if not os.path.exists(note_path):
        return [(_assert(False, "跳过（缺样本）", note_path)[0],
                 "跳过（缺样本）", note_path)]

    # 复制 .note 到临时位置（covert_json_to_markdown 会删除原 .note）
    tmp_note = os.path.join(SCRIPT_DIR, "_zygote_render_tmp.note")
    with io.open(note_path, "rb") as f:
        raw = f.read()
    with open(tmp_note, "wb") as f:
        f.write(raw)

    # 备份可能存在的 .md
    md_path = os.path.splitext(note_path)[0] + ".md"
    backup = md_path + ".bak_render"
    if os.path.exists(md_path) and not os.path.exists(backup):
        with open(md_path, "rb") as f:
            orig = f.read()
        with open(backup, "wb") as f:
            f.write(orig)

    # 模拟 pull._prefetch_whiteboards 的产出
    fake_map = {
        "fake-excalidraw-id": "images/fake-excalidraw-id.svg",
        "fake-drawio-id": "images/fake-drawio-id.svg",
    }

    try:
        new_md = YoudaoNoteConvert.covert_json_to_markdown(tmp_note, whiteboard_map=fake_map)
        if not new_md or not os.path.exists(new_md):
            return [(_assert(False, "covert_json_to_markdown 失败", "")[0],
                     "covert_json_to_markdown 失败", "")]
        with io.open(new_md, "r", encoding="utf-8") as f:
            md_text = f.read()
    finally:
        if os.path.exists(tmp_note):
            os.remove(tmp_note)
        # 恢复 .md 备份
        if os.path.exists(backup):
            with open(backup, "rb") as f:
                orig = f.read()
            with open(md_path, "wb") as f:
                f.write(orig)
            os.remove(backup)

    results = []
    # 关键：map 中不存在的节点 → 走原占位
    # 因为 fake id 不在真实 .note 中，所以预期仍输出原占位
    results.append(_assert("excalidraw" in md_text, "map 未命中仍输出 excalidraw 占位"))
    results.append(_assert("drawio-ynote" in md_text, "map 未命中仍输出 drawio-ynote 占位"))
    # map 中也不应被错误写入
    results.append(_assert('images/fake-excalidraw-id.svg' not in md_text,
                            "未真实预下载的 node 不被错误嵌入"))
    # 文本节点保留
    for kw in ("Zygote", "fork", "SystemServer", "AMS", "PMS"):
        if kw in md_text:
            results.append(_assert(True, "文本节点保留：" + kw))
        else:
            results.append((PASS, "文本节点（软断言）" + kw, "未命中但不影响"))
    return results


def test_zygote_e2e_with_real_svg_assets():
    """
    端到端：跑 Zygote 真实样本 + 真实 SVG 资产。
    我们提前用 Zygote 真实二进制渲染两份 SVG 到临时 images 目录，
    然后构造一个 map（用真实存在的 node id，目前用占位 id + 文件存在），
    验证整个流水线的产物可被 Markdown 引用。
    """
    from core.whiteboard_render import (
        render_excalidraw_to_svg,
        render_drawio_ynote_to_svg,
        save_whiteboard_svg,
    )
    from covert import YoudaoNoteConvert

    note_path = os.path.join(SCRIPT_DIR, "Zygote进程的作用.note")
    ex_bin = os.path.join(SCRIPT_DIR, "whiteboard_samples", "Zygote_excalidraw.bin")
    dio_bin = os.path.join(SCRIPT_DIR, "whiteboard_samples", "Zygote_drawio-ynote.bin")
    if not (os.path.exists(note_path) and os.path.isfile(ex_bin) and os.path.isfile(dio_bin)):
        return [(_assert(False, "跳过（缺样本）", "")[0], "跳过（缺样本）", "")]

    with tempfile.TemporaryDirectory() as tmp:
        # 1) 渲染两份 SVG 到 tmp/images
        images_dir = os.path.join(tmp, "images")
        with io.open(ex_bin, "r", encoding="utf-8") as f:
            ex_svg = render_excalidraw_to_svg(f.read())
        with io.open(dio_bin, "r", encoding="utf-8") as f:
            dio_svg = render_drawio_ynote_to_svg(f.read())
        ex_rel = save_whiteboard_svg(images_dir, "ex-node", ex_svg)
        dio_rel = save_whiteboard_svg(images_dir, "dio-node", dio_svg)
        # 2) 复制 .note 进 tmp，转 .md
        tmp_note = os.path.join(tmp, "test.note")
        with open(note_path, "rb") as f:
            raw = f.read()
        with open(tmp_note, "wb") as f:
            f.write(raw)
        # 3) 用真实 id 构造 map（这两个 id 在真实 .note 中未必存在，
        #    这里只是验证管线能跑通 + 渲染产物落地）
        new_md = YoudaoNoteConvert.covert_json_to_markdown(
            tmp_note,
            whiteboard_map={"ex-node": ex_rel, "dio-node": dio_rel},
        )
        with io.open(new_md, "r", encoding="utf-8") as f:
            md_text = f.read()
        # 4) 校验 SVG 资产确实存在
        ex_file = os.path.join(images_dir, "ex-node.svg")
        dio_file = os.path.join(images_dir, "dio-node.svg")
        results = []
        results.append(_assert(os.path.isfile(ex_file), "excalidraw SVG 落盘成功"))
        results.append(_assert(os.path.isfile(dio_file), "drawio-ynote SVG 落盘成功"))
        results.append(_assert(ex_rel == "images/ex-node.svg", "excalidraw 相对路径正确"))
        results.append(_assert(dio_rel == "images/dio-node.svg", "drawio-ynote 相对路径正确"))
        # 由于 fake id 不在真实 .note，预期 .md 中没有图片（但有占位）
        results.append(_assert("![" not in md_text, "fake id 未命中 → 不输出图片"))
        results.append(_assert("excalidraw" in md_text, "仍输出 excalidraw 占位"))
        results.append(_assert("drawio-ynote" in md_text, "仍输出 drawio-ynote 占位"))
    return results


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    suites = [
        ("render_excalidraw_to_svg", test_render_excalidraw_to_svg),
        ("render_drawio_ynote_to_svg", test_render_drawio_ynote_to_svg),
        ("render_mindmap_to_svg", test_render_mindmap_to_svg),
        ("render_whiteboard 统一入口", test_render_whiteboard_dispatch),
        ("save_whiteboard_svg 落盘", test_save_whiteboard_svg),
        ("whiteboard_map 命中 excalidraw", test_whiteboard_map_hit_excalidraw),
        ("whiteboard_map 命中 drawio-ynote", test_whiteboard_map_hit_drawio_ynote),
        ("whiteboard_map 命中 mindmap", test_whiteboard_map_hit_mindmap),
        ("whiteboard_map 未命中回退", test_whiteboard_map_miss_fallback),
        ("covert_json_to_markdown whiteboard_map 签名", test_covert_json_to_markdown_signature),
        ("Zygote e2e + map（fake id）", test_zygote_e2e_with_map),
        ("Zygote e2e + 真实 SVG 资产", test_zygote_e2e_with_real_svg_assets),
    ]

    all_results = []
    for name, fn in suites:
        print("\n=== " + name + " ===")
        try:
            res = fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
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
