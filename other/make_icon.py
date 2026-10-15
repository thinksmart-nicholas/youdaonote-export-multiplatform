# -*- coding: utf-8 -*-
"""生成项目图标：体现「批量导出有道云笔记」。

构图（自上而下的叙事）：
    云端云朵  →  向下的导出箭头  →  叠放的两张笔记卡片（= 批量）

产出（位于本脚本同目录 other/）：
    icon.png   1024x1024  （Linux 启动器 / .desktop 使用）
    icon.ico   多尺寸     （Windows）
    icon.icns  多尺寸     （macOS）

仅开发者需要重新生成图标时运行：
    python other/make_icon.py
"""
import io
import os
import struct
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 1024

TOP = (0x5B, 0x9D, 0xFF)   # 渐变上沿（天蓝）
BOT = (0x1B, 0x54, 0xD6)   # 渐变下沿（深蓝）
WHITE = (255, 255, 255, 255)
LINE = (0x7F, 0xA8, 0xFF)  # 卡片上的文字线（浅蓝）
RADIUS = 220               # 背景圆角

# Freedesktop 图标主题规范（hicolor）常用尺寸
HICOLOR_SIZES = (16, 22, 24, 32, 48, 64, 128, 256, 512)
ICON_NAME = "YoudaoNoteExporter"


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def rounded_mask(size, radius):
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def gradient_bg(size):
    """垂直渐变背景（圆角）。"""
    col = Image.new("RGB", (1, 256))
    px = col.load()
    for y in range(256):
        px[0, y] = _lerp(TOP, BOT, y / 255.0)
    img = col.resize((size, size), Image.BILINEAR).convert("RGBA")
    img.putalpha(rounded_mask(size, int(RADIUS * size / SIZE)))
    return img


def draw_cloud(d, cx, y0, y1, k=1.0):
    """云朵：三个圆 + 一个圆角底座。"""
    w = (y1 - y0)
    r_big = int(0.52 * w)
    r_sm = int(0.38 * w)
    cy_big = y0 + r_big
    cy_sm = y0 + int(0.62 * w)
    for cx_i, cy_i, r in (
        (cx, cy_big, r_big),
        (cx - int(0.60 * w), cy_sm, r_sm),
        (cx + int(0.60 * w), cy_sm, r_sm),
    ):
        d.ellipse([cx_i - r, cy_i - r, cx_i + r, cy_i + r], fill=WHITE)
    # 底座
    half = int(0.98 * w / 2 * 2)
    d.rounded_rectangle(
        [cx - half // 2, cy_sm - int(0.05 * w), cx + half // 2, y1],
        radius=int(0.20 * w), fill=WHITE)


def draw_arrow(d, cx, y_top, y_bot, shaft_w, head_w):
    """向下的导出箭头：圆角竖杆 + 三角箭头。"""
    head_h = head_w
    d.rounded_rectangle(
        [cx - shaft_w // 2, y_top, cx + shaft_w // 2, y_bot - head_h // 2],
        radius=shaft_w // 2, fill=WHITE)
    d.polygon(
        [(cx - head_w, y_bot - head_h), (cx + head_w, y_bot - head_h), (cx, y_bot)],
        fill=WHITE)


def draw_card(d, box, radius, lines=3):
    """笔记卡片：白色圆角矩形 + 若干文字线。"""
    x0, y0, x1, y1 = box
    d.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=WHITE)
    lw = int((x1 - x0) * 0.62)
    lh = max(2, int((y1 - y0) * 0.075))
    gap = int((y1 - y0) * 0.16)
    top = y0 + int((y1 - y0) * 0.22)
    for i in range(lines):
        ly = top + i * (lh + gap)
        d.rounded_rectangle(
            [x0 + int((x1 - x0) * 0.13), ly,
             x0 + int((x1 - x0) * 0.13) + lw, ly + lh],
            radius=lh // 2, fill=LINE)


def make_base(size=SIZE):
    s = size / float(SIZE)   # 缩放系数

    def sc(v):
        return int(v * s)

    img = gradient_bg(size)
    d = ImageDraw.Draw(img)
    cx = size // 2

    # 云端云朵（上部）
    draw_cloud(d, cx, sc(180), sc(360))

    # 向下导出箭头（中部）
    draw_arrow(d, cx, sc(400), sc(660), sc(38), sc(112))

    # 叠放的两张笔记卡片（下部）= 批量
    cw = sc(460)           # 卡片宽
    ch = sc(230)           # 卡片高
    off = sc(50)           # 后卡片的偏移量
    front = [cx - cw // 2, sc(650), cx + cw // 2, sc(650) + ch]
    back = [front[0] + off, sc(595), front[2] + off, sc(595) + ch]
    draw_card(d, back, sc(34), lines=2)
    draw_card(d, front, sc(34), lines=3)

    return img


def save_ico(img, path, sizes=(16, 32, 48, 64, 128, 256)):
    img.save(path, "ICO", sizes=[(s, s) for s in sizes])


def build_icns(img, path):
    """最简 ICNS：用 PNG 数据填充 ic07/ic08/ic09/ic10 条目。"""
    targets = {b"ic07": 128, b"ic08": 256, b"ic09": 512, b"ic10": 1024}
    entries = b""
    for os_type, s in targets.items():
        buf = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        buf.paste(img.resize((s, s), Image.LANCZOS))
        png = io.BytesIO()
        buf.save(png, "PNG")
        data = png.getvalue()
        entries += os_type + struct.pack(">I", len(data) + 8) + data
    out = b"icns" + struct.pack(">I", len(entries) + 8) + entries
    with open(path, "wb") as f:
        f.write(out)


def save_hicolor(img, root):
    """按 Freedesktop 图标主题规范生成 hicolor 多尺寸树：

        <root>/icons/hicolor/<size>x<size>/apps/YoudaoNoteExporter.png

    桌面环境据此在菜单 / Dock / 文件管理器等不同尺寸场景自动取用，
    避免只用一个大图被强行缩放导致模糊。
    """
    made = []
    for s in HICOLOR_SIZES:
        d = os.path.join(root, "icons", "hicolor", "%dx%d" % (s, s), "apps")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, ICON_NAME + ".png")
        img.resize((s, s), Image.LANCZOS).save(p, "PNG")
        made.append(p)
    return made


def main():
    base = make_base(SIZE)
    base.save(os.path.join(HERE, "icon.png"), "PNG")
    save_ico(base, os.path.join(HERE, "icon.ico"))
    build_icns(base, os.path.join(HERE, "icon.icns"))
    save_hicolor(base, HERE)
    print("icons generated: icon.png / icon.ico / icon.icns")
    print("hicolor sizes: %s" % ", ".join(str(s) for s in HICOLOR_SIZES))


if __name__ == "__main__":
    main()
