# -*- coding: utf-8 -*-
"""
白板 / 流程图 渲染模块

把有道云笔记中的白板节点（excalidraw / drawio-ynote / mindmap）转换成
可缩放的 SVG 图片文件，并内嵌到 Markdown 中。

设计原则：
- 最小侵入：不对外暴露 push / pull 主流程的内部数据结构，只新增纯函数
- 容错优先：任意解析失败都不影响主流程，调用方负责兜底
- 输出统一：都返回 SVG 文本或 (svg_path, markdown_img) 路径对，便于 Markdown 内嵌
- 跨平台：纯标准库实现，不依赖任何第三方 SVG 库
"""

import base64
import hashlib
import html
import json
import math
import os
import re
import struct

# 内部模块（按现有项目结构相对引用）
try:
    from core.log import logger
except Exception:  # 单独 import 也允许
    import logging

    logger = logging.getLogger("whiteboard_render")


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# excalidraw 渲染默认字体大小
_EX_FONT_SIZE = 20

# 文本行高（px）
_EX_LINE_HEIGHT = 1.25

# 文本与节点矩形之间的内边距
_EX_TEXT_PADDING = 6

# SVG 画布额外留白
_EX_CANVAS_PAD = 20

# 箭头默认 stroke 粗细
_EX_STROKE_WIDTH = 2

# 字体（兼容 windows / mac / linux / 移动端浏览器）
_EX_FONT_FAMILY = "Helvetica, Arial, 'Microsoft YaHei', 'PingFang SC', sans-serif"

# 等宽字体：excalidraw fontFamily = 3（Cascadia）。注意 3 表示等宽而非粗体，
# 旧实现误把它当成 bold，这里只映射到等宽字体族。
_EX_FONT_FAMILY_CODE = "'Cascadia Code', Consolas, 'Courier New', monospace"

# excalidraw fontFamily -> CSS font-family。1（Virgil 手绘体）无通用字体替代，
# 与 2 一样沿用现有无衬线字体，保证与改动前观感一致。
_EX_FONT_FAMILY_BY_ID = {3: _EX_FONT_FAMILY_CODE}

# 行内基线在行高内的经验比例。仅当元素缺少官方 baseline 字段时兜底使用：
# 真实样本中 baseline/行高 稳定落在 0.72~0.75 之间。
_EX_BASELINE_RATIO = 0.75

# excalidraw 支持的位图元素所引用的 files 字段键名
_EX_FILE_ID_KEYS = ("fileId", "fileid", "fileID")

# data URI 的 MIME -> 落盘扩展名
_MIME_EXT = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
    "image/svg+xml": "svg",
}

# 是否优先使用有道服务端渲染好的官方图片。官方图 100% 还原字体与线条样式
# （Virgil 手绘字体、hachure 填充、rough 线条），但 excalidraw 的官方图是 1x
# 位图，放大超过原尺寸会模糊。置 False 可退回自研矢量渲染（可无损缩放）。
_PREFER_OFFICIAL_IMAGE = True

# 优先走官方图的白板类型。drawio-ynote 的 content 字段本身就是官方矢量 SVG，
# 保持原路径不变，避免引入无谓风险。
_OFFICIAL_FIRST_TYPES = ("excalidraw", "mindmap")


# ---------------------------------------------------------------------------
# 资源下载（依赖外部 api session）
# ---------------------------------------------------------------------------

def download_whiteboard_content(api_session, url):
    """
    下载白板原始资源。

    :param api_session: core.api.YoudaoNoteApi 实例（有 .session 内部 requests.Session）
    :param url: 资源 URL
    :return: 解码后的字符串内容（失败时返回空串）
    """
    if not url or not api_session:
        return ""
    try:
        # 复用 api 内部 session 发起 GET
        resp = api_session.session.get(url, timeout=30)
        if resp.status_code != 200:
            logger.warning("下载白板资源失败 status=%s url=%s", resp.status_code, url)
            return ""
        # 资源本身可能是 utf-8 也可能是 gbk，逐个试
        raw = resp.content
        for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("下载白板资源异常 url=%s err=%s", url, e)
        return ""


# ---------------------------------------------------------------------------
# drawio-ynote 渲染
# ---------------------------------------------------------------------------

# drawio-ynote 资源最外层 JSON 形如 {"content": "data:image/svg+xml;base64,...."}
_DATA_URI_RE = re.compile(r"^data:image/svg\+xml;base64,(.*)$", re.DOTALL)


def render_drawio_ynote_to_svg(content_text):
    """
    drawio-ynote 资源 -> SVG 文本。

    资源结构：外层 JSON 含 content 字段，是 data:image/svg+xml;base64,xxx 形式。
    """
    if not content_text:
        return ""
    try:
        outer = json.loads(content_text)
    except Exception:
        # 退化：直接尝试作为 data URI 解析
        outer = {"content": content_text}

    # 白板底色，用于给「导出时不带背景」的 SVG 补底
    background = (outer.get("background") or "") if isinstance(outer, dict) else ""

    data_uri = ""
    if isinstance(outer, dict):
        # 优先级：content > svg > data
        for key in ("content", "svg", "data"):
            val = outer.get(key)
            if isinstance(val, str) and val.startswith("data:image/svg+xml;base64,"):
                data_uri = val
                break
    elif isinstance(outer, str) and outer.startswith("data:image/svg+xml;base64,"):
        data_uri = outer

    if not data_uri:
        # 也许 content 字段已经是裸 base64
        if isinstance(outer, dict):
            for key in ("content", "svg", "data"):
                val = outer.get(key)
                if isinstance(val, str) and "<svg" in val.lower():
                    return _normalize_svg(val, background)
        return ""

    m = _DATA_URI_RE.match(data_uri)
    if not m:
        return ""
    try:
        svg_bytes = base64.b64decode(m.group(1))
    except Exception as e:
        logger.warning("drawio-ynote base64 解码失败: %s", e)
        return ""
    try:
        svg_text = svg_bytes.decode("utf-8")
    except UnicodeDecodeError:
        svg_text = svg_bytes.decode("utf-8", errors="replace")
    return _normalize_svg(svg_text, background)


def _svg_has_background(svg_text):
    """判断 SVG 是否已有不透明背景（根标签背景色，或覆盖整个 viewBox 的背景矩形）。"""
    start = svg_text.find("<svg")
    if start == -1:
        return False
    end = svg_text.find(">", start)
    if end != -1 and "background-color" in svg_text[start:end].lower():
        return True
    vb = re.search(r'viewBox="([^"]+)"', svg_text)
    if not vb:
        return False
    try:
        parts = [float(v) for v in vb.group(1).replace(",", " ").split()]
        vw, vh = parts[2], parts[3]
    except (ValueError, IndexError):
        return False
    for m in re.finditer(r"<rect\b[^>]*>", svg_text, re.I):
        tag = m.group(0)
        if not re.search(r'\bfill="(?!none|transparent)[^"]*"', tag, re.I):
            continue
        mw = re.search(r'\bwidth="([^"]+)"', tag)
        mh = re.search(r'\bheight="([^"]+)"', tag)
        if not (mw and mh):
            continue
        try:
            rw = float(mw.group(1).rstrip("px"))
            rh = float(mh.group(1).rstrip("px"))
        except ValueError:
            continue
        if abs(rw - vw) < 1 and abs(rh - vh) < 1:
            return True
    return False


def _ensure_svg_background(svg_text, background="#ffffff"):
    """
    SVG 缺少背景时补一层与 viewBox 等大的背景矩形。

    流程图 SVG（drawio / Visio / Mermaid / ProcessOn 导出）经常不带背景：内联
    在白色页面里看不出问题，但被 lightbox / PhotoSwipe / fancybox 等预览组件
    放到深色遮罩上后，透明区域会直接透出深色，图上的深色线条与文字就看不见了。
    """
    if not svg_text or "<svg" not in svg_text:
        return svg_text
    if _svg_has_background(svg_text):
        return svg_text
    vb = re.search(r'viewBox="([^"]+)"', svg_text)
    if not vb:
        return svg_text
    try:
        parts = [float(v) for v in vb.group(1).replace(",", " ").split()]
        x, y, w, h = parts[0], parts[1], parts[2], parts[3]
    except (ValueError, IndexError):
        return svg_text
    if w <= 0 or h <= 0:
        return svg_text
    rect = (
        f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" '
        f'fill="{_ex_color(background, "#ffffff")}" stroke="none"/>'
    )
    idx = svg_text.find(">", svg_text.find("<svg")) + 1
    return svg_text[:idx] + rect + svg_text[idx:]


def _svg_viewbox_size(svg_text):
    """从 viewBox 解析 (width, height)，解析失败返回 (0.0, 0.0)。"""
    m = re.search(r'viewBox="([^"]+)"', svg_text)
    if not m:
        return 0.0, 0.0
    try:
        parts = [float(v) for v in m.group(1).replace(",", " ").split()]
        return parts[2], parts[3]
    except (ValueError, IndexError):
        return 0.0, 0.0


def _normalize_svg(svg_text, background="#ffffff"):
    """
    规范化 SVG：
    - 去掉内嵌 mxfile content 属性（与缩放无关，减少冗余）
    - 保证有 xmlns
    - 强制 width/height 设为 100%（让容器决定实际大小，更利于滚轮/触摸缩放）
    - 缺失背景时补一层与 viewBox 等大的背景矩形

    :param background: 背景色，取自白板资源的 background 字段

    所有改写都只作用于根 <svg> 的起始标签。旧实现用 count=1 在整篇文档上做
    正则替换，一旦根标签缺少 width/height，就会命中内部第一个子图元，把它
    的尺寸改成 100% 导致图形变形。
    """
    if not svg_text or "<svg" not in svg_text:
        return svg_text
    start = svg_text.find("<svg")
    end = svg_text.find(">", start)
    if end == -1:
        return svg_text
    head = svg_text[start:end]
    # 去掉 content="..." 整个属性（避免 mxfile 大块 base64 冗余）
    head = re.sub(r'\s+content="[^"]*"', "", head)
    # 给根 svg 加 xmlns（如果缺失）
    if 'xmlns="http://www.w3.org/2000/svg"' not in head:
        head = head.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    # 宽高必须是像素值，不能写百分比：百分比会让 SVG 失去固有尺寸，语雀等平台
    # 导入时无法确定图片大小，浏览器里也会被当成默认 300x150 的替换元素。
    # 优先保留原始像素尺寸，取不到再从 viewBox 推导。
    vw, vh = _svg_viewbox_size(head)
    w_attr = "%g" % vw if vw > 0 else "100%"
    h_attr = "%g" % vh if vh > 0 else "100%"
    head = re.sub(r'\swidth="[^"]*"', ' width="%s"' % w_attr, head)
    head = re.sub(r'\sheight="[^"]*"', ' height="%s"' % h_attr, head)
    if 'width="' not in head:
        head += ' width="%s"' % w_attr
    if 'height="' not in head:
        head += ' height="%s"' % h_attr
    # 加上 preserveAspectRatio 与样式让浏览器/移动端可缩放
    if "preserveAspectRatio" not in head:
        head = head.replace("<svg", '<svg preserveAspectRatio="xMidYMid meet"', 1)
    return _ensure_svg_background(
        svg_text[:start] + head + svg_text[end:], background
    )


# ---------------------------------------------------------------------------
# 官方渲染兜底（有道服务端已渲染好的白板位图）
# ---------------------------------------------------------------------------

def decode_data_uri(data_uri):
    """
    解析 data URI，返回 (扩展名, 字节内容)；不是支持的类型或解码失败返回 ("", b"")。
    """
    if not data_uri or not isinstance(data_uri, str):
        return "", b""
    m = re.match(
        r"^data:([a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+);base64,(.*)$",
        data_uri.strip(),
        re.DOTALL,
    )
    if not m:
        return "", b""
    ext = _MIME_EXT.get(m.group(1).lower(), "")
    if not ext:
        return "", b""
    try:
        data = base64.b64decode(m.group(2))
    except Exception as e:
        logger.warning("data URI base64 解码失败: %s", e)
        return "", b""
    if not data:
        return "", b""
    if ext == "svg":
        # 官方矢量图也过一遍规范化（剥除 mxfile 冗余、补 xmlns 与自适应尺寸）
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        data = _normalize_svg(text).encode("utf-8")
    return ext, data


def extract_official_image(content_text):
    """
    从白板资源里取出服务端渲染好的官方图片。

    有道返回的外层 JSON 中 svg 字段就是官方渲染结果：excalidraw 是 PNG 位图
    （含 Virgil 手绘字体、hachure 填充、rough 线条），drawio / mindmap 多为
    矢量 SVG。直接落盘即可 100% 还原官方样式，不依赖任何本地字体或矢量库。

    :param content_text: download_whiteboard_content 取回的资源文本
    :return: (扩展名, 字节内容)，取不到返回 ("", b"")
    """
    if not content_text:
        return "", b""
    try:
        outer = json.loads(content_text)
    except Exception:
        return "", b""
    if isinstance(outer, dict):
        return decode_data_uri(outer.get("svg"))
    return "", b""


def _ex_png_size(data_uri):
    """
    从 PNG data URI 解析 (width, height)，失败返回 (0, 0)。

    只读 IHDR 前 8 字节的宽高字段，避免引入 PIL 等第三方依赖。
    """
    try:
        m = re.match(r"^data:image/png;base64,([A-Za-z0-9+/=]+)", str(data_uri).strip())
        if not m:
            return 0, 0
        head = base64.b64decode(m.group(1)[:64])
        if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
            return 0, 0
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    except Exception:
        return 0, 0


def _ex_raster_svg(data_uri, background=""):
    """
    把官方渲染好的白板位图（PNG data URI）包成 SVG，作为自研矢量渲染的兜底。

    有道服务端返回的外层 svg 字段是权威渲染结果，坐标/字体/换行 100% 准确。
    当自研渲染拿不到可用内容（无元素、元素类型全不支持、画布范围为空）时直接
    采用它，避免白板整体丢失。返回空串表示连兜底素材也没有。
    """
    if not data_uri or not str(data_uri).startswith("data:image"):
        return ""
    w, h = _ex_png_size(data_uri)
    if w <= 0 or h <= 0:
        # 解析不出尺寸时给一个常见画布，至少保证图片可见
        w, h = 800, 600
    bg = _ex_color(background, "#ffffff") if background else "#ffffff"
    uri = _ex_escape(data_uri)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
        f'preserveAspectRatio="xMidYMid meet">'
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="{bg}" stroke="none"/>'
        f'<image x="0" y="0" width="{w}" height="{h}" '
        f'href="{uri}" xlink:href="{uri}" />'
        f'</svg>'
    )


# ---------------------------------------------------------------------------
# excalidraw 渲染
# ---------------------------------------------------------------------------

# 颜色 -> 中文友好名映射
_EX_COLOR = {
    "#1e1e1e": "#1e1e1e",
    "#ffffff": "#ffffff",
    "#f8f9fa": "#f8f9fa",
    "#1971c2": "#1971c2",
    "#2f9e44": "#2f9e44",
    "#e03131": "#e03131",
    "#f08c00": "#f08c00",
    "#9c36b5": "#9c36b5",
    "#0c8599": "#0c8599",
    "#5c940d": "#5c940d",
    "#c2255c": "#c2255c",
    "#5f3dc4": "#5f3dc4",
    "transparent": "none",
}


def _ex_color(c, default="#1e1e1e"):
    if not c:
        return default
    return _EX_COLOR.get(str(c).lower(), c)


def _ex_escape(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _ex_num(v, default=0.0):
    """把 excalidraw 字段安全转成 float（width/height 等常出现小数或 None）。"""
    if v is None or isinstance(v, bool):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _ex_font_family(font_family_id):
    """excalidraw fontFamily -> CSS font-family 字符串。"""
    try:
        return _EX_FONT_FAMILY_BY_ID.get(int(font_family_id), _EX_FONT_FAMILY)
    except (TypeError, ValueError):
        return _EX_FONT_FAMILY


def _ex_opacity_attr(el):
    """excalidraw opacity 取值 0~100，只在非完全不透明时输出 opacity 属性。"""
    op = _ex_num(el.get("opacity"), 100)
    if op >= 100:
        return ""
    return f' opacity="{max(0.0, op / 100.0):.3f}"'


def _ex_rotate_attr(el, cx, cy):
    """元素有 angle（弧度）时输出 rotate 变换，绕元素中心旋转。"""
    angle = _ex_num(el.get("angle"))
    if not angle:
        return ""
    deg = angle * 180.0 / math.pi
    return f' transform="rotate({deg:.2f} {cx:.2f} {cy:.2f})"'


def _ex_wrap_text(text, font_size, max_width):
    """
    按 max_width 字符宽度简单换行（中文按 1 字符宽度，英文按 0.55 字符宽度估算）。
    """
    if not text:
        return [""]
    if max_width <= 0:
        return [str(text)]

    out_lines = []
    for raw_line in str(text).splitlines() or [""]:
        if not raw_line:
            out_lines.append("")
            continue
        cur = ""
        cur_w = 0
        for ch in raw_line:
            if ord(ch) > 127:
                w = font_size
            else:
                w = font_size * 0.55
            if cur_w + w > max_width and cur:
                out_lines.append(cur)
                cur = ch
                cur_w = w
            else:
                cur += ch
                cur_w += w
        if cur:
            out_lines.append(cur)
    return out_lines or [""]


def _ex_parse_typed_text(raw_text, default_fs=_EX_FONT_SIZE):
    """
    解析 excalidraw 的 text 字段：
    - 纯字符串 -> [(text, default_style)]，字号取元素自身 fontSize（不再硬编码 20）
    - [{"type":"text", "text":"...", "fontSize":20}, ...] -> 保留多段样式

    :param default_fs: 元素自身 fontSize，纯字符串文本没有携带字号时使用
    """
    base_fs = _ex_num(default_fs, _EX_FONT_SIZE) or _EX_FONT_SIZE
    if raw_text is None:
        return [("", base_fs, False, False)], base_fs
    if isinstance(raw_text, str):
        return [(raw_text, base_fs, False, False)], base_fs
    if not isinstance(raw_text, list):
        return [(str(raw_text), base_fs, False, False)], base_fs

    segments = []
    max_fs = base_fs
    for seg in raw_text:
        if not isinstance(seg, dict):
            continue
        t = seg.get("text", "")
        # 用 float 而非 int：excalidraw 会存 20.84 这类非整数字号，截断会累积行距误差
        fs = _ex_num(seg.get("fontSize"), base_fs) or base_fs
        max_fs = max(max_fs, fs)
        bold = bool(seg.get("fontWeight") == "bold" or seg.get("bold"))
        italic = bool(seg.get("fontStyle") == "italic" or seg.get("italic"))
        segments.append((t, fs, bold, italic))
    if not segments:
        return [("", base_fs, False, False)], base_fs
    return segments, max_fs


def _ex_split_segments_to_lines(segments, max_width):
    """
    把多段 excalidraw 文本拆成多行（保留每段独立 fontSize）。

    :param max_width: 换行的可用宽度；<= 0 表示不折行。
                      excalidraw 的 text 已带官方换行点，且 width/height 是精确
                      测量值，此时必须传 0 原样保留换行，否则行数变化会让基线
                      与垂直对齐计算全部偏移。
    """
    lines = []  # [(text, fontSize, bold, italic)]
    for text, fs, bold, italic in segments:
        raw = str(text).replace("\r\n", "\n").replace("\r", "\n")
        raw_lines = raw.split("\n")
        for line in raw_lines:
            # 空行必须保留：旧实现只在 lines 为空时保留，会静默吞掉段中空行
            if not line:
                lines.append(("", fs, bold, italic))
                continue
            for w in _ex_wrap_text(line, fs, max_width):
                lines.append((w, fs, bold, italic))
    if not lines:
        lines = [("", _EX_FONT_SIZE, False, False)]
    return lines


def _ex_rect_svg(x, y, w, h, stroke, fill, stroke_w=_EX_STROKE_WIDTH, rx=0, ry=0):
    return (
        f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
        f'rx="{rx}" ry="{ry}" '
        f'fill="{_ex_color(fill, "none")}" stroke="{_ex_color(stroke)}" '
        f'stroke-width="{stroke_w}"/>'
    )


def _ex_ellipse_svg(x, y, w, h, stroke, fill, stroke_w=_EX_STROKE_WIDTH):
    cx = x + w / 2
    cy = y + h / 2
    rx = w / 2
    ry = h / 2
    return (
        f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" '
        f'fill="{_ex_color(fill, "none")}" stroke="{_ex_color(stroke)}" '
        f'stroke-width="{stroke_w}"/>'
    )


def _ex_diamond_svg(x, y, w, h, stroke, fill, stroke_w=_EX_STROKE_WIDTH):
    pts = [
        (x + w / 2, y),
        (x + w, y + h / 2),
        (x + w / 2, y + h),
        (x, y + h / 2),
    ]
    points = " ".join(f"{px:.2f},{py:.2f}" for px, py in pts)
    return (
        f'<polygon points="{points}" '
        f'fill="{_ex_color(fill, "none")}" stroke="{_ex_color(stroke)}" '
        f'stroke-width="{stroke_w}"/>'
    )


def _ex_text_svg(x, y, lines, anchor="start", baseline="alphabetic"):
    """
    渲染多行文字。
    """
    if not lines:
        return ""
    tspans = []
    for i, (text, fs, bold, italic) in enumerate(lines):
        weight = "bold" if bold else "normal"
        style = "italic" if italic else "normal"
        dy = ("0" if i == 0 else f"{fs * _EX_LINE_HEIGHT:.2f}")
        tspans.append(
            f'<tspan x="{x:.2f}" dy="{dy}">{_ex_escape(text)}</tspan>'
        )
    return (
        f'<text x="{x:.2f}" y="{y:.2f}" '
        f'font-family="{_EX_FONT_FAMILY}" '
        f'text-anchor="{anchor}" dominant-baseline="{baseline}" '
        f'fill="{_ex_color("#1e1e1e")}" stroke="none">'
        f'{"".join(tspans)}'
        f'</text>'
    )


def _ex_group(inner, attrs):
    """需要 opacity / rotate 等属性时把图元包一层 <g>，没有则原样返回。"""
    if not inner or not attrs:
        return inner
    return f"<g{attrs}>{inner}</g>"


def _ex_arrow_head_svg(p_tip, p_tail, stroke, size):
    """在 p_tip 处画一个指向 p_tail -> p_tip 方向的三角箭头。"""
    dx = p_tip[0] - p_tail[0]
    dy = p_tip[1] - p_tail[1]
    length = math.hypot(dx, dy)
    if length <= 0:
        return ""
    ux, uy = dx / length, dy / length
    # 箭头两翼
    left = (
        p_tip[0] - ux * size + (-uy) * size * 0.4,
        p_tip[1] - uy * size + ux * size * 0.4,
    )
    right = (
        p_tip[0] - ux * size - (-uy) * size * 0.4,
        p_tip[1] - uy * size - ux * size * 0.4,
    )
    tri = f"{p_tip[0]:.2f},{p_tip[1]:.2f} {left[0]:.2f},{left[1]:.2f} {right[0]:.2f},{right[1]:.2f}"
    return (
        f'<polygon points="{tri}" fill="{_ex_color(stroke)}" stroke="{_ex_color(stroke)}" '
        f'stroke-width="1"/>'
    )


def _ex_arrow_svg(points, stroke, stroke_w=_EX_STROKE_WIDTH, start_head=None):
    """
    根据 points 数组 [(x,y), ...] 渲染折线 + 箭头。

    :param start_head: 起始箭头类型（excalidraw startArrowhead）；None 表示不画，
                       保持与旧行为一致（只画末端箭头）。
    """
    if not points or len(points) < 2:
        return ""
    poly = " ".join(f"{p[0]:.2f},{p[1]:.2f}" for p in points)
    out = [
        f'<polyline points="{poly}" fill="none" '
        f'stroke="{_ex_color(stroke)}" stroke-width="{stroke_w}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    ]
    size = max(8, stroke_w * 4)
    if start_head:
        out.append(_ex_arrow_head_svg(points[0], points[1], stroke, size))
    # 箭头
    out.append(_ex_arrow_head_svg(points[-1], points[-2], stroke, size))
    return "".join(out)


def _ex_line_svg(points, stroke, stroke_w=_EX_STROKE_WIDTH):
    if not points or len(points) < 2:
        return ""
    poly = " ".join(f"{p[0]:.2f},{p[1]:.2f}" for p in points)
    return (
        f'<polyline points="{poly}" fill="none" '
        f'stroke="{_ex_color(stroke)}" stroke-width="{stroke_w}" '
        f'stroke-linecap="round" stroke-linejoin="round"/>'
    )


def render_excalidraw_to_svg(content_text):
    """
    excalidraw 资源 -> SVG 文本。

    资源结构（与 drawio-ynote 相同）：
        外层 JSON: {"content": "<内层字符串>", "svg": "...", "background": "..."}
        content 字段是 escape 后的 JSON 字符串，里面才是 excalidraw 真结构：
            {"type":"excalidraw", "version":2, "elements":[...]}
        支持元素类型：rectangle / ellipse / diamond / text / arrow / line
    """
    if not content_text:
        return ""
    # 1) 解析外层 JSON，找 content 字段
    try:
        outer = json.loads(content_text)
    except Exception as e:
        logger.warning("excalidraw 外层 JSON 解析失败: %s", e)
        return ""

    if isinstance(outer, dict):
        # 外层 svg 字段是服务端渲染好的权威位图，background 是白板底色。
        # 旧实现只取 content 自己重画，把它们全丢了。
        fallback_raster = outer.get("svg") or ""
        background = outer.get("background") or ""
        inner = outer.get("content")
        if isinstance(inner, str):
            content_text = inner
        elif isinstance(inner, dict):
            # 已经是 dict
            return _render_excalidraw_from_data(inner, fallback_raster, background)
    elif isinstance(outer, list):
        return _render_excalidraw_from_data(outer)

    # 2) 解析内层 excalidraw
    try:
        data = json.loads(content_text)
    except Exception as e:
        logger.warning("excalidraw 内层 JSON 解析失败: %s", e)
        # 内层坏掉时仍有官方位图可用，避免白板整块丢失
        return _ex_raster_svg(fallback_raster, background)
    return _render_excalidraw_from_data(data, fallback_raster, background)


def _ex_image_svg(el, x, y, w, h, files):
    """
    渲染 excalidraw 的 image 元素：从 data["files"] 取内嵌资源。

    旧实现完全不处理 image 类型，白板里的插图会整块丢失。
    """
    if w <= 0 or h <= 0:
        return ""
    href = ""
    for key in _EX_FILE_ID_KEYS:
        file_id = el.get(key)
        if not file_id:
            continue
        item = files.get(file_id)
        if isinstance(item, dict):
            href = item.get("dataURL") or ""
        elif isinstance(item, str):
            href = item
        if href:
            break
    if not str(href).startswith(("data:", "http://", "https://")):
        return ""
    return (
        f'<image x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
        f'href="{_ex_escape(href)}" preserveAspectRatio="none"/>'
    )


def _ex_text_element_svg(el, x, y, w, h, fallback_width=0):
    """
    渲染单个 excalidraw text 元素（含正确的基线与行高）。

    基线规则（与 excalidraw 一致）：
      - 元素的 baseline 字段 = 元素顶边到「最后一行」基线的偏移，是权威值；
      - 行高 = height / 行数。真实样本里行高未必等于 fontSize * 1.25，
        直接按 1.25 估算会让多行文本整体漂移；
      - 首行基线 = y + baseline - (行数 - 1) * 行高。

    旧实现忽略 baseline 与 verticalAlign、把字号固定成 20、并按伪 measureText
    额外折行，实测 8 个文本块全部偏移 10~182 px。
    """
    fs = _ex_num(el.get("fontSize"), _EX_FONT_SIZE) or _EX_FONT_SIZE
    segments, _ = _ex_parse_typed_text(el.get("text"), fs)
    # excalidraw 的 width 是精确测量宽度，text 自带官方换行点：
    # 有宽度时原样保留换行（wrap_w <= 0 表示不折行），多折一行就会让
    # 行高与基线全部算错
    wrap_w = 0 if w > 0 else fallback_width
    flat_lines = _ex_split_segments_to_lines(segments, wrap_w)
    n_lines = len(flat_lines)

    line_h = (h / n_lines) if (h > 0 and n_lines > 0) else (fs * _EX_LINE_HEIGHT)

    baseline = el.get("baseline")
    if baseline is not None and _ex_num(baseline, -1) >= 0:
        y_baseline = y + _ex_num(baseline) - (n_lines - 1) * line_h
    else:
        # 缺少 baseline 字段时按 verticalAlign 兜底
        v_align = el.get("verticalAlign") or "top"
        content_h = line_h * n_lines
        if h > 0 and v_align == "middle":
            top = (h - content_h) / 2
        elif h > 0 and v_align == "bottom":
            top = h - content_h
        else:
            top = 0
        y_baseline = y + top + line_h * _EX_BASELINE_RATIO

    # excalidraw textAlign(left/center/right) -> SVG text-anchor(start/middle/end)。
    # SVG 的 text-anchor 没有 "center" 这个合法值，直接写会被判为非法并回退成
    # start，居中文本就会从中心点向右展开、整体右偏约半个字宽并溢出所属方框。
    anchor = {"left": "start", "center": "middle", "right": "end"}.get(
        el.get("textAlign") or "center", "middle"
    )
    if anchor == "middle":
        tx = x + w / 2
    elif anchor == "end":
        tx = x + w
    else:
        tx = x

    family = _ex_font_family(el.get("fontFamily"))
    color = _ex_color(el.get("strokeColor") or "#1e1e1e")
    out = [
        f'<text x="{tx:.2f}" y="{y_baseline:.2f}" '
        f'font-family="{family}" text-anchor="{anchor}" '
        f'fill="{color}" stroke="none">'
    ]
    for i, (text, line_fs, bold, italic) in enumerate(flat_lines):
        weight = "bold" if bold else "normal"
        style = "italic" if italic else "normal"
        dy = "0" if i == 0 else f"{line_h:.2f}"
        out.append(
            f'<tspan x="{tx:.2f}" dy="{dy}" font-size="{line_fs:g}" '
            f'font-weight="{weight}" font-style="{style}">{_ex_escape(text)}</tspan>'
        )
    out.append("</text>")
    return "".join(out)


def _render_excalidraw_from_data(data, fallback_raster="", background=""):
    """
    实际 excalidraw JSON 数据 -> SVG 文本。

    :param fallback_raster: 外层官方渲染好的位图 data URI；自研渲染拿不到可用
                            内容时用它兜底，避免白板整体丢失
    :param background: 白板背景色
    """
    if not isinstance(data, dict):
        return _ex_raster_svg(fallback_raster, background)

    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    raw_elements = data.get("elements") or data.get("shapes") or []
    if not isinstance(raw_elements, list):
        raw_elements = []

    # 过滤已删除元素：旧实现不检查 isDeleted，已删内容既会被画出来又会撑大画布
    elements = [e for e in raw_elements if isinstance(e, dict) and not e.get("isDeleted")]
    if not elements:
        return _ex_raster_svg(fallback_raster, background)

    # 计算画布范围：所有元素类型都参与，避免「只剩 freedraw / image 等类型」时
    # bbox 为空导致整块白板被丢弃
    xs, ys = [], []
    for el in elements:
        ex = _ex_num(el.get("x"))
        ey = _ex_num(el.get("y"))
        pts = el.get("points")
        if isinstance(pts, list) and pts:
            for p in pts:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    xs.append(ex + _ex_num(p[0]))
                    ys.append(ey + _ex_num(p[1]))
        else:
            ew = _ex_num(el.get("width"))
            eh = _ex_num(el.get("height"))
            xs.extend([ex, ex + ew])
            ys.extend([ey, ey + eh])
    if not xs or not ys:
        return _ex_raster_svg(fallback_raster, background)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    width = max(50, max_x - min_x)
    height = max(50, max_y - min_y)
    pad = _EX_CANVAS_PAD

    body = []
    # 按 elements 原始顺序单遍绘制：excalidraw 的 z-order 就是数组顺序。
    # 旧实现分两遍（先画所有图形、再画所有文本）会把本该在下层的文本提到最上层。
    for el in elements:
        et = el.get("type")
        x = _ex_num(el.get("x")) - min_x + pad
        y = _ex_num(el.get("y")) - min_y + pad
        w = _ex_num(el.get("width"))
        h = _ex_num(el.get("height"))
        stroke = el.get("strokeColor") or "#1e1e1e"
        fill = el.get("backgroundColor") or "transparent"
        # 元素自身 strokeWidth（旧实现固定为 2，线宽比例会失真）
        stroke_w = _ex_num(el.get("strokeWidth"), _EX_STROKE_WIDTH) or _EX_STROKE_WIDTH
        attrs = _ex_opacity_attr(el) + _ex_rotate_attr(el, x + w / 2, y + h / 2)

        if et in ("rectangle", "ellipse", "diamond"):
            if et == "rectangle":
                inner = _ex_rect_svg(x, y, w, h, stroke, fill, stroke_w)
            elif et == "ellipse":
                inner = _ex_ellipse_svg(x, y, w, h, stroke, fill, stroke_w)
            else:
                inner = _ex_diamond_svg(x, y, w, h, stroke, fill, stroke_w)
        elif et in ("arrow", "line", "freedraw"):
            pts = []
            for p in el.get("points") or []:
                if isinstance(p, (list, tuple)) and len(p) >= 2:
                    pts.append((x + _ex_num(p[0]), y + _ex_num(p[1])))
            if et == "arrow":
                inner = _ex_arrow_svg(pts, stroke, stroke_w, el.get("startArrowhead"))
            else:
                # line / freedraw（手绘笔迹）都渲染为折线；旧实现直接丢弃 freedraw
                inner = _ex_line_svg(pts, stroke, stroke_w)
        elif et == "image":
            inner = _ex_image_svg(el, x, y, w, h, files)
        elif et == "text":
            inner = _ex_text_element_svg(el, x, y, w, h, width)
        else:
            # frame / embeddable / iframe / magicframe / selection 等：
            # 已参与画布范围计算，但不绘制，避免破坏版面
            continue

        if inner:
            body.append(_ex_group(inner, attrs))

    if not body:
        return _ex_raster_svg(fallback_raster, background)

    canvas_w = width + 2 * pad
    canvas_h = height + 2 * pad
    bg = _ex_color(background, "#ffffff") if background else "#ffffff"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {canvas_w:.2f} {canvas_h:.2f}" '
        f'width="{canvas_w:.2f}" height="{canvas_h:.2f}" '
        f'preserveAspectRatio="xMidYMid meet" '
        f'font-family="{_EX_FONT_FAMILY}">'
        f'<rect x="0" y="0" width="{canvas_w:.2f}" height="{canvas_h:.2f}" '
        f'fill="{bg}" stroke="none"/>'
        f'{"".join(body)}'
        f'</svg>'
    )


# ---------------------------------------------------------------------------
# mindmap 渲染（占位）
# ---------------------------------------------------------------------------

def render_mindmap_to_svg(content_text):
    """
    mindmap 资源 -> SVG 文本（占位实现）。

    真实结构未确认，目前输出简单提示 SVG，避免白板节点完全空白。
    即使 content_text 为空也返回一个友好的占位 SVG（避免节点显示空）。
    """
    data = {}
    if content_text:
        try:
            data = json.loads(content_text)
        except Exception:
            data = {}

    # 尝试提取一些节点信息作为提示
    title = ""
    nodes = []
    if isinstance(data, dict):
        title = str(data.get("title") or data.get("name") or "思维导图")
        raw_nodes = data.get("nodes") or data.get("children") or []
        if isinstance(raw_nodes, list):
            for n in raw_nodes[:20]:
                if isinstance(n, dict):
                    nodes.append(str(n.get("text") or n.get("title") or n.get("name") or ""))
                elif isinstance(n, str):
                    nodes.append(n)
    else:
        title = "思维导图"

    lines = [title] if title else ["思维导图"]
    lines.extend([n for n in nodes if n][:10])

    body = []
    y = 30
    body.append(
        f'<rect x="0" y="0" width="600" height="{40 + 28 * len(lines)}" '
        f'fill="#fafafa" stroke="#cccccc" stroke-width="1"/>'
    )
    for i, t in enumerate(lines):
        size = 20 if i == 0 else 14
        weight = "bold" if i == 0 else "normal"
        body.append(
            f'<text x="20" y="{y + i * 28}" font-family="{_EX_FONT_FAMILY}" '
            f'font-size="{size}" font-weight="{weight}" fill="#1e1e1e">{_ex_escape(t)}</text>'
        )
    h = 40 + 28 * len(lines)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 {h}" '
        f'width="100%" height="100%" preserveAspectRatio="xMidYMid meet" '
        f'font-family="{_EX_FONT_FAMILY}">'
        f'{"".join(body)}'
        f'</svg>'
    )


# ---------------------------------------------------------------------------
# 统一调度
# ---------------------------------------------------------------------------

# 类型 -> 渲染函数
_RENDERERS = {
    "excalidraw": render_excalidraw_to_svg,
    "drawio-ynote": render_drawio_ynote_to_svg,
    "mindmap": render_mindmap_to_svg,
}


def render_whiteboard(type_name, content_text):
    """
    统一入口：按 type_name 选择渲染器并返回 SVG 文本。
    失败时返回空串。
    """
    func = _RENDERERS.get(type_name)
    if not func:
        return ""
    try:
        return func(content_text) or ""
    except Exception as e:
        logger.warning("渲染白板失败 type=%s err=%s", type_name, e)
        return ""


# ---------------------------------------------------------------------------
# 保存到 images 目录并产出 Markdown 引用
# ---------------------------------------------------------------------------

def _safe_node_name(node_id):
    """
    把节点 id 清洗成安全文件名（去掉路径分隔符与 Windows 保留字符）。

    不同 id 可能被清洗成同一名字（如 a/b 与 a:b 都变成 a_b），此时追加短哈希
    避免白板互相覆盖。
    """
    raw_id = str(node_id)
    safe_id = re.sub(r'[\\/:*?"<>|]', "_", raw_id)
    if safe_id != raw_id:
        safe_id = "{}_{}".format(
            safe_id, hashlib.md5(raw_id.encode("utf-8")).hexdigest()[:8]
        )
    return safe_id


def save_whiteboard_svg(images_dir, node_id, svg_text):
    """
    把 SVG 文本保存到 images_dir/<node_id>.svg，返回相对路径 images/<node_id>.svg。

    :param images_dir: 绝对路径（一般是 <笔记目录>/images）
    :param node_id: 节点 id
    :param svg_text: SVG 字符串
    :return: 相对路径（写入 Markdown 用）；失败返回空串
    """
    if not svg_text or not node_id:
        return ""
    try:
        if not os.path.isdir(images_dir):
            os.makedirs(images_dir, exist_ok=True)
        safe_id = _safe_node_name(node_id)
        file_path = os.path.join(images_dir, f"{safe_id}.svg")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(svg_text)
        # 返回相对路径
        return f"images/{safe_id}.svg"
    except Exception as e:
        logger.warning("保存白板 SVG 失败 id=%s err=%s", node_id, e)
        return ""


def save_whiteboard_image(images_dir, node_id, image_data, ext="png"):
    """
    把官方渲染好的图片字节保存到 images_dir/<node_id>.<ext>，
    返回相对路径 images/<node_id>.<ext>；失败返回空串。

    :param images_dir: 绝对路径（一般是 <笔记目录>/images）
    :param node_id: 节点 id
    :param image_data: 图片字节内容（PNG / JPEG / SVG 等）
    :param ext: 扩展名（不含点）
    :return: 相对路径（写入 Markdown 用）；失败返回空串
    """
    if not image_data or not node_id or not ext:
        return ""
    try:
        if not os.path.isdir(images_dir):
            os.makedirs(images_dir, exist_ok=True)
        safe_id = _safe_node_name(node_id)
        file_path = os.path.join(images_dir, "{}.{}".format(safe_id, ext))
        with open(file_path, "wb") as f:
            f.write(image_data)
        return "images/{}.{}".format(safe_id, ext)
    except Exception as e:
        logger.warning("保存白板图片失败 id=%s err=%s", node_id, e)
        return ""


def process_and_save(api_session, type_name, url, images_dir, node_id,
                     prefer_official=None):
    """
    一站式：下载 + 渲染 + 保存。
    返回相对路径（images/<id>.<ext>）或空串。

    :param prefer_official: 是否优先落盘服务端官方渲染图。None 表示沿用模块
                            默认 _PREFER_OFFICIAL_IMAGE。官方图能 100% 还原字体
                            与线条样式；但 excalidraw 的官方图是 1x 位图，放大到
                            超过原尺寸会模糊，置 False 可退回自研矢量渲染。
    """
    if not url or not type_name or not node_id:
        return ""
    if prefer_official is None:
        prefer_official = _PREFER_OFFICIAL_IMAGE
    content = download_whiteboard_content(api_session, url)
    if not content:
        return ""
    # 官方渲染优先：直接落盘服务端渲染好的图，样式与有道客户端完全一致
    if prefer_official and type_name in _OFFICIAL_FIRST_TYPES:
        ext, image_data = extract_official_image(content)
        if image_data:
            rel_path = save_whiteboard_image(images_dir, node_id, image_data, ext)
            if rel_path:
                return rel_path
    svg = render_whiteboard(type_name, content)
    if not svg:
        return ""
    return save_whiteboard_svg(images_dir, node_id, svg)
