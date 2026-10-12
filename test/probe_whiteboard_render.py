# -*- coding: utf-8 -*-
"""
白板渲染快速验证：直接喂样本二进制，输出 SVG 文本长度 + 前 200 字符。
不联网、不落盘，仅验证 whiteboard_render.py 渲染正确。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.whiteboard_render import (
    render_excalidraw_to_svg,
    render_drawio_ynote_to_svg,
    render_mindmap_to_svg,
    render_whiteboard,
    save_whiteboard_svg,
)

EX_BIN = os.path.join(ROOT, "test", "whiteboard_samples", "Zygote_excalidraw.bin")
DIO_BIN = os.path.join(ROOT, "test", "whiteboard_samples", "Zygote_drawio-ynote.bin")


def main():
    print("=" * 60)
    print("excalidraw 渲染")
    print("=" * 60)
    if os.path.isfile(EX_BIN):
        with open(EX_BIN, "r", encoding="utf-8") as f:
            content = f.read()
        svg = render_excalidraw_to_svg(content)
        print(f"输入长度: {len(content)} 字节")
        print(f"输出 SVG 长度: {len(svg)} 字节")
        print(f"输出前 300 字符: {svg[:300]}")
    else:
        print(f"样本不存在: {EX_BIN}")

    print()
    print("=" * 60)
    print("drawio-ynote 渲染")
    print("=" * 60)
    if os.path.isfile(DIO_BIN):
        with open(DIO_BIN, "r", encoding="utf-8") as f:
            content = f.read()
        svg = render_drawio_ynote_to_svg(content)
        print(f"输入长度: {len(content)} 字节")
        print(f"输出 SVG 长度: {len(svg)} 字节")
        print(f"输出前 300 字符: {svg[:300]}")
    else:
        print(f"样本不存在: {DIO_BIN}")

    print()
    print("=" * 60)
    print("render_whiteboard 统一入口（未知 type 兜底）")
    print("=" * 60)
    print(f"未知 type 返回: {repr(render_whiteboard('foo', '{}'))}")
    print(f"空内容返回: {repr(render_whiteboard('excalidraw', ''))}")

    print()
    print("=" * 60)
    print("save_whiteboard_svg 落盘")
    print("=" * 60)
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        if os.path.isfile(EX_BIN):
            with open(EX_BIN, "r", encoding="utf-8") as f:
                content = f.read()
            svg = render_excalidraw_to_svg(content)
            rel = save_whiteboard_svg(tmp, "node-abc/123", svg)
            print(f"excalidraw 落盘相对路径: {rel}")
            print(f"  实际文件: {os.path.join(tmp, os.path.basename(rel))}")
            print(f"  文件存在: {os.path.isfile(os.path.join(tmp, os.path.basename(rel)))}")
            print(f"  文件大小: {os.path.getsize(os.path.join(tmp, os.path.basename(rel)))} 字节")

        if os.path.isfile(DIO_BIN):
            with open(DIO_BIN, "r", encoding="utf-8") as f:
                content = f.read()
            svg = render_drawio_ynote_to_svg(content)
            rel = save_whiteboard_svg(tmp, "node-xyz", svg)
            print(f"drawio-ynote 落盘相对路径: {rel}")
            print(f"  实际文件: {os.path.join(tmp, os.path.basename(rel))}")
            print(f"  文件存在: {os.path.isfile(os.path.join(tmp, os.path.basename(rel)))}")
            print(f"  文件大小: {os.path.getsize(os.path.join(tmp, os.path.basename(rel)))} 字节")


if __name__ == "__main__":
    main()
