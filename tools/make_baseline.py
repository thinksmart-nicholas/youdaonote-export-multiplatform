#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Golden 基线工具：为 covert 模块重构提供「前后行为一致」的比对依据。

用法::

    python tools/make_baseline.py             # 采集基线 -> tools/baseline/
    python tools/make_baseline.py --compare   # 用当前代码重新转换并与基线逐字节比对

原理:
    把样本 .note 复制到临时目录后交给 YoudaoNoteConvert 转换，
    以输出 .md 的字节内容与 sha256 作为比对依据。

覆盖范围:
    仅覆盖纯本地的坐标转换（XML / JSON / HTML -> Markdown），
    不覆盖需要网络与登录态的图片、附件迁移和白板预下载。

样本类型判定沿用 pull.py 的逻辑:
    <?xml  -> XML；{" -> JSON；其它 -> 其它（不转换）。
    且 XML 分支遇到 ET.ParseError 时同样回落 HTML 转换。
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.covert import YoudaoNoteConvert  # noqa: E402

BASELINE_DIR = os.path.join(ROOT, "tools", "baseline")
MANIFEST_PATH = os.path.join(BASELINE_DIR, "manifest.json")

SAMPLE_ROOTS = ("test", "youdaonote")
WALK_EXCLUDE_DIRS = ("images", "__pycache__", "whiteboard_samples", "logs", "venv3.14")


def _iter_note_samples():
    """遍历样本目录下的 .note 文件（相对仓库根目录的路径）"""
    for root in SAMPLE_ROOTS:
        abs_root = os.path.join(ROOT, root)
        if not os.path.isdir(abs_root):
            continue
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d not in WALK_EXCLUDE_DIRS]
            for filename in filenames:
                if filename.lower().endswith(".note"):
                    yield os.path.join(dirpath, filename)


def _iter_json_samples():
    """test 目录下以 '{' 开头的 .json 文件，作为 JSON 分支样本"""
    test_dir = os.path.join(ROOT, "test")
    if not os.path.isdir(test_dir):
        return
    for filename in sorted(os.listdir(test_dir)):
        if not filename.lower().endswith(".json"):
            continue
        path = os.path.join(test_dir, filename)
        try:
            with open(path, "rb") as f:
                head = _read_head(path)
        except OSError:
            continue
        if RE_JSON_HEAD.match(head):
            yield path


def _key_of(abs_path):
    """把样本的绝对路径映射为基线文件名（目录分隔符扁平化）"""
    rel = os.path.relpath(abs_path, ROOT)
    return rel.replace(os.sep, "__") + ".md"


NOISE_START = b"\xef\xbb\xbf"  # UTF-8 BOM，某些有道导出的 JSON 前会带上
RE_JSON_HEAD = re.compile(rb'^\{\s*"')  # {"..." / { "..." 均视为 JSON 笔记


def _read_head(note_path, size=512):
    """读取文件头部字节并去掉 BOM 与首尾空白，用于类型判定"""
    with open(note_path, "rb") as f:
        head = f.read(size)
    if head.startswith(NOISE_START):
        head = head[len(NOISE_START):]
    return head.strip()


def _judge_kind(note_path):
    """判定样本类型，返回 xml / json / html / other

    说明：这里比 pull.py 的判定多出 html 一类。
    pull.py 只对 <?xml / {" 两种开头做转换，早年的 HTML 老笔记会落到
    FileType.OTHER 而被原样保留；但 covert_html_to_markdown 仍是
    本项目的重要组成部分（supports ET.ParseError 回落路径），
    因此本工具把「看起来是 HTML」的样本显式喂给它，保证重构时有覆盖。
    """
    head = _read_head(note_path)
    if head[:5] == b"<?xml":
        return "xml"
    if RE_JSON_HEAD.match(head):
        return "json"
    if head.startswith(b"<") and re.search(rb"^<(html|body|div|p|h[1-6]|span|ul|ol|table)\b", head, re.I):
        return "html"
    return "other"


def convert_one(note_path):
    """按 pull.py 的分发逻辑转换单个样本。

    :return: (status, detail)
        status: "converted" 已转换 / "skipped" 非可判类型，未转换
        detail: 说明字符串（转换函数 / 跳过原因）
    """
    kind = _judge_kind(note_path)
    if kind == "xml":
        try:
            YoudaoNoteConvert.covert_xml_to_markdown(note_path)
            return "converted", "covert_xml_to_markdown"
        except ET.ParseError:
            YoudaoNoteConvert.covert_html_to_markdown(note_path)
            return "converted", "covert_html_to_markdown"
    if kind == "json":
        YoudaoNoteConvert.covert_json_to_markdown(note_path)
        return "converted", "covert_json_to_markdown"
    if kind == "html":
        YoudaoNoteConvert.covert_html_to_markdown(note_path)
        return "converted", "covert_html_to_markdown"
    return "skipped", "not xml/json/html"


def run_all():
    """对全部样本执行转换，返回 {key: {"sha256":..., "size":..., "status":..., "detail":..., "bytes":...}}"""
    results = {}
    samples = list(_iter_note_samples()) + list(_iter_json_samples())
    for abs_path in samples:
        key = _key_of(abs_path)
        tmp_dir = tempfile.mkdtemp(prefix="ydbaseline_")
        try:
            tmp_note = os.path.join(tmp_dir, os.path.basename(abs_path))
            shutil.copy2(abs_path, tmp_note)
            base = os.path.splitext(tmp_note)[0]
            try:
                status, detail = convert_one(tmp_note)
            except Exception as err:  # 转换异常本身也是行为的一部分，记录而非中断
                results[key] = {
                    "status": "error",
                    "detail": "{}: {}".format(type(err).__name__, err),
                    "sha256": "",
                    "size": 0,
                    "bytes": b"",
                }
                continue

            # other 类型不转换：原样记录源文件内容作为基线
            md_path = base + ".md"
            if status == "skipped" or not os.path.isfile(md_path):
                source_path = base + os.path.splitext(abs_path)[1]
                if os.path.isfile(source_path):
                    with open(source_path, "rb") as f:
                        payload = f.read()
                else:
                    payload = b""
            else:
                with open(md_path, "rb") as f:
                    payload = f.read()

            results[key] = {
                "status": status,
                "detail": detail,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "bytes": payload,
            }
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
    return results


def save_baseline(results):
    if os.path.isdir(BASELINE_DIR):
        shutil.rmtree(BASELINE_DIR, ignore_errors=True)
    os.makedirs(BASELINE_DIR, exist_ok=True)

    manifest = {}
    for key, info in sorted(results.items()):
        with open(os.path.join(BASELINE_DIR, key), "wb") as f:
            f.write(info["bytes"])
        manifest[key] = {
            "sha256": info["sha256"],
            "size": info["size"],
            "status": info["status"],
            "detail": info["detail"],
        }
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print("基线已写入 {}（{} 个样本）".format(BASELINE_DIR, len(manifest)))


def compare(results):
    if not os.path.isfile(MANIFEST_PATH):
        print("未找到基线：{}，请先执行 python tools/make_baseline.py".format(MANIFEST_PATH))
        return 2
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    ok, diff, missing = 0, [], []
    for key, old in sorted(manifest.items()):
        new = results.get(key)
        if new is None:
            missing.append(key)
            continue
        if old["sha256"] == new["sha256"] and old["status"] == new["status"]:
            ok += 1
        else:
            diff.append((key, old, new))

    print("一致 {} / 基线 {}；差异 {}；缺失 {}".format(
        ok, len(manifest), len(diff), len(missing)
    ))
    extra = [k for k in results if k not in manifest]
    if extra:
        print("新增样本（基线未覆盖）: {}".format(", ".join(sorted(extra))))

    for key, old, new in diff:
        print("\n[DIFF] {}".format(key))
        if old["status"] != new["status"]:
            print("  status: {} -> {}".format(old["status"], new["status"]))
        if old["detail"] != new["detail"]:
            print("  detail: {} -> {}".format(old["detail"], new["detail"]))
        if old["sha256"] != new["sha256"]:
            print("  sha256: {} -> {}".format(old["sha256"], new["sha256"]))
            old_bytes = b""
            baseline_file = os.path.join(BASELINE_DIR, key)
            if os.path.isfile(baseline_file):
                with open(baseline_file, "rb") as f:
                    old_bytes = f.read()
            _print_byte_diff(old_bytes, new["bytes"])
    for key in missing:
        print("\n[MISSING] {}".format(key))

    return 1 if (diff or missing) else 0


def _print_byte_diff(old_bytes, new_bytes):
    """打印前若干处差异的上下文，便于定位回归点"""
    old_lines = old_bytes.split(b"\n")
    new_lines = new_bytes.split(b"\n")
    shown = 0
    limit = min(len(old_lines), len(new_lines))
    for i in range(limit):
        if old_lines[i] != new_lines[i]:
            print("  line {}:".format(i + 1))
            print("    - {}".format(old_lines[i][:160]))
            print("    + {}".format(new_lines[i][:160]))
            shown += 1
            if shown >= 5:
                break
    if len(old_lines) != len(new_lines):
        print("  line count: {} -> {}".format(len(old_lines), len(new_lines)))


def main():
    parser = argparse.ArgumentParser(description="covert 模块的 golden 基线工具")
    parser.add_argument(
        "--compare", action="store_true", help="与既有基线比对，而非重新采集基线"
    )
    args = parser.parse_args()

    results = run_all()
    if args.compare:
        return compare(results)
    save_baseline(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
