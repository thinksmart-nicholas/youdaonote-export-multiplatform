# -*- coding: utf-8 -*-
"""Markdown 生成后的统一后处理链（顺序固定，勿随意调整）。"""

import base64
import hashlib
import logging
import os
import re

from .indent import _HEUR_FONT_SIZE_RE, _has_any_native_indent, restore_heirarchy_indent  # noqa: F401
from .util import PRESERVE_NATIVE_INDENT, _MAX_INLINE_IMAGE_BYTES, _MIME_TO_EXT, _REGEX_BASE64_IMAGE  # noqa: F401



def _externalize_oversized_base64_images(md_file_path, max_bytes=_MAX_INLINE_IMAGE_BYTES):
    """扫描 Markdown 文件，将超过大小阈值的内联 base64 图片解码保存为本地文件，
    并替换为相对路径引用，用于兼容 Typora 对超长 base64 单行解析失败的问题。

    浏览器、语雀对超长 base64 无压力，因此此处理不影响它们的显示效果；
    而 Typora 只要遇到 ~100K 字符以上的单行 data-URI，就会跳过 Markdown 解析
    直接显示原始源码。把大图外置为相对路径后，所有渲染器都 100% 兼容。

    :param md_file_path: md 文件绝对路径
    :param max_bytes:   内联最大字节数（原始图像），默认 32KB
    :return:            处理的图片张数（仅外置了的），int
    """
    if not os.path.isfile(md_file_path):
        return 0

    with open(md_file_path, "rb") as f:
        raw_bytes = f.read()
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = raw_bytes.decode("utf-8", errors="ignore")

    if "data:image/" not in content:
        return 0  # 快速跳过：无任何 base64 图片

    md_dir = os.path.dirname(os.path.abspath(md_file_path))
    md_stem = os.path.splitext(os.path.basename(md_file_path))[0]
    images_dir = os.path.join(md_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    processed = 0
    img_counter = [0]  # 用可变容器让闭包内可修改

    def _replacer(match):
        alt = match.group(1)
        mime = match.group(2).lower().strip()
        b64_raw = match.group(3)
        # 清理 base64 中可能混入的换行/空格（有些导出链路会写入换行）
        b64_clean = re.sub(r"\s+", "", b64_raw)
        if len(b64_clean) % 4:
            b64_clean += "=" * (4 - len(b64_clean) % 4)

        # 先按字符长度快速估算：1 base64 字符≈0.75字节
        if len(b64_clean) * 3 // 4 <= max_bytes:
            # 可能未超阈值，精确解码后再判断
            try:
                decoded = base64.b64decode(b64_clean, validate=False)
            except Exception:
                return match.group(0)  # 解码失败：原样保留
            if len(decoded) <= max_bytes:
                return match.group(0)  # 小图：保留内联
        else:
            try:
                decoded = base64.b64decode(b64_clean, validate=False)
            except Exception:
                return match.group(0)

        ext = _MIME_TO_EXT.get(mime, ".png")
        # 文件名：md文件名_序号_SHA256前8位，保证唯一（同图复用、不同图不冲突）
        short_hash = hashlib.sha256(decoded).hexdigest()[:8]
        img_counter[0] += 1
        filename = "{}_{:03d}_{}{}".format(md_stem, img_counter[0], short_hash, ext)
        img_path = os.path.join(images_dir, filename)
        try:
            with open(img_path, "wb") as f:
                f.write(decoded)
        except OSError as err:
            logging.warning("保存外置图片失败：%s → %s（%s），保留原内联",
                            filename, img_path, err)
            return match.group(0)

        # 相对路径：images/filename
        rel_path = "images/" + filename
        nonlocal processed
        processed += 1
        logging.info("已将超大 base64 图片外置为本地文件：%s（%.1f KB，%s）",
                     rel_path, len(decoded) / 1024, mime)
        return "![{}]({})".format(alt, rel_path)

    new_content = _REGEX_BASE64_IMAGE.sub(_replacer, content)
    if processed > 0 and new_content != content:
        with open(md_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
    return processed


# ---------- 软换行合并（有道笔记段内换行被导出拆成独立段落）----------
# 有道笔记的软换行（段内 Shift+Enter）在导出时被变成独立段落，
# 导致一句话中途被拆成多行。此函数在 restore_heirarchy_indent 之前运行，
# 处理的是纯 span 段落（可能已被 restore 加过 \t/- 前缀的历史文件也兼容）。
# 两种检测模式：
#   1. 行尾 \t+ 在 </span> 内（有道软换行行尾特征）→ 与后续同样式 span 行合并
#   2. 编号文本 N.xxx + 同样式非编号文本（中间仅 1 空行）→ 合并

# 匹配一行：可选列表前缀(\t*- 或 \t+) + <span ...>text</span>
_RE_LINE_SPAN = re.compile(r'^(\t*(?:- )?)(<span\b[^>]*>)(.*?)(</span>)\s*$')
# 本轮把行首缩进从"tab 前缀"改成了块级 <div style="padding-left:NNpx">…</div> 和行首 inline
# <span style="padding-left:NNpx"></span> 空容器 → 软换行合并前必须先"剥掉外层壳"才能让
# 旧的 _RE_LINE_SPAN / _RE_TRAILING_TABS / _RE_NUMBERED 继续生效。
# ⚠️：不要把这两个"壳"的正则写成一行共享插值（\g 会在 replace 时污染匹配组号）。
_RE_PAD_DIV_WRAP = re.compile(
    r'^(\s*(?:<span\b[^>]*style="[^"]*padding-left\s*:\s*\d{2,}px\b[^>]*>\s*</span>)*\s*)'
    r'<div\b([^>]*\bstyle="[^"]*padding-left\s*:\s*\d{2,}px\b[^"]*"[^>]*)>(.*?)</div>\s*$',
    re.IGNORECASE | re.DOTALL)
_RE_PAD_SPAN_LINEHEAD_STRIP = re.compile(
    r'^(<span\b[^>]*style="[^"]*padding-left\s*:\s*\d{2,}px\b[^>]*>\s*</span>)*\s*',
    re.IGNORECASE)
_RE_TRAILING_TABS = re.compile(r'(\t+)\s*$')
_RE_NUMBERED = re.compile(r'^\d+[\.．、]')


def _extract_style(span_open_tag):
    """从 <span style="..."> 提取 style 值，无则返回空串。"""
    m = re.search(r'style="([^"]*)"', span_open_tag)
    return m.group(1) if m else ""


def _peel_pad_wrapper(bare_line: str) -> tuple[str, callable]:
    """剥掉本轮写的"缩进壳"（<div style="padding-left:NNpx">…</div> 以及
    它前面可能存在的任意 padding span 空容器），返回 (内层 body, 重包函数)。
    不命中任何壳时：(bare_line, lambda x:x)。
    内层 body 可能是：纯 span 段落 / 老 `\t*(- )?<span>…</span>` / 内联 padding span + 列表项。"""
    if not bare_line:
        return bare_line, lambda x: x
    md = _RE_PAD_DIV_WRAP.match(bare_line)
    if md:
        lead_spans = md.group(1) or ""
        div_attrs = md.group(2) or ""
        inner = md.group(3)
        open_div = "{}<div{}>".format(lead_spans, div_attrs)
        close_div = "</div>"
        # 重包：剥离/合并后产出的内层文本仍要放回原 div 容器里，否则缩进会丢。
        # 如果内层被拼接成多段（极罕见），每段单独包（保持 1 行=1 缩进级 div）。
        def rewrap(new_inner: str) -> str:
            # new_inner 若是空：保持空
            if new_inner == "":
                return ""
            return "{}{}{}".format(open_div, new_inner, close_div)
        return inner, rewrap
    return bare_line, lambda x: x


def _merge_soft_linebreaks(md_file_path) -> int:
    """合并有道笔记软换行被导出拆成的多个段落。

    在 restore_heirarchy_indent 之前运行。
    兼容三种"一行里 span 的外壳"状态：
      ① 历史遗留：纯 `\t*(- )?<span>…</span>`（零壳）
      ② 本轮 XML/JSON para 缩进外包：`<div style="padding-left:NNpx"><span>…</span></div>`
      ③ 本轮行首内联缩进（list-item / HTML MDTAB）：`<span style="padding-left:NNpx"></span>- <span>…</span>`
    处理流程：先用 `_peel_pad_wrapper()` 剥掉 ② 的 div 外壳（再用 `_RE_PAD_SPAN_LINEHEAD_STRIP`
    剥 ③ 的行首 padding span），把剥前的前缀/外壳记住 → 用原有 Pattern 1/2 合并内层 →
    合并完成后把前缀/外壳包回去。"""
    if not os.path.isfile(md_file_path):
        return 0
    with open(md_file_path, "rb") as f:
        raw = f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="ignore")

    lines = content.split("\n")
    merged_count = 0
    i = 0
    while i < len(lines):
        orig = lines[i].rstrip("\r")
        # ---------- 阶段 A：剥外层（div + lead padding spans）----------
        body_a, rewrap_a = _peel_pad_wrapper(orig)
        # 再剥 body_a 行首可能存在的 inline padding span 前缀（③）→ 裸 <span> 行
        sp = _RE_PAD_SPAN_LINEHEAD_STRIP.match(body_a)
        head_a = sp.group(0) if sp else ""
        core_a = body_a[len(head_a):] if head_a else body_a
        # head_a 非空且不含 padding-only spans？其实不影响：把它当前缀原样保留就行。

        m = _RE_LINE_SPAN.match(core_a)
        if not m:
            i += 1
            continue

        list_prefix, open_tag, span_text, close_tag = m.groups()
        style_a = _extract_style(open_tag)

        def rewrap_all(new_list_prefix: str, new_open: str, new_text: str, new_close: str) -> str:
            """把"新的 list_prefix / open / text / close"按顺序装回 lead / div 外壳。"""
            new_core = new_list_prefix + new_open + new_text + new_close
            new_body = "{}{}".format(head_a, new_core)
            return rewrap_a(new_body)

        # --- Pattern 1: trailing \t inside span text ---
        tab_match = _RE_TRAILING_TABS.search(span_text)
        if tab_match:
            clean_text = span_text[:tab_match.start()]
            merged_text = clean_text
            consumed = []
            # 向后收集 同 壳/同样式 span 行（跳过空行）
            k = i + 1
            while k < len(lines):
                if lines[k].strip() == "":
                    k += 1
                    continue
                break
            while k < len(lines):
                orig_k = lines[k].rstrip("\r")
                body_k, rewrap_k = _peel_pad_wrapper(orig_k)
                sk_m = _RE_PAD_SPAN_LINEHEAD_STRIP.match(body_k)
                head_k = sk_m.group(0) if sk_m else ""
                core_k = body_k[len(head_k):] if head_k else body_k
                mk = _RE_LINE_SPAN.match(core_k)
                if not mk:
                    # 兼容"外壳不同但核心 span 相同"的情况（一般不会，保守 break）。
                    break
                _, k_open, k_text, k_close = mk.groups()
                if _extract_style(k_open) != style_a:
                    break
                # 外层壳/前缀必须匹配，否则合并后缩进会错（比如 metrics L1 不能把 L2 的段落并进来）。
                # 简化判定：同样的 div_attrs（通过 rewrap 的字符串相同判断太脆）；
                # 改用"合并后下一项仍然独立：如不同层则不合并"——这里用 style_a 相同 + core 本身
                # 结构（open_tag 字符数一致且 style 相同）已做，再加一个"视觉缩进像素匹配"就够：
                # 如果 div_attr 里的 padding-left px 都相等就算可合并。
                px_a = None; px_k = None
                mpa = re.search(r'padding-left\s*:\s*(\d{2,})px', orig, flags=re.IGNORECASE)
                if mpa: px_a = int(mpa.group(1))
                mpk = re.search(r'padding-left\s*:\s*(\d{2,})px', orig_k, flags=re.IGNORECASE)
                if mpk: px_k = int(mpk.group(1))
                if px_a != px_k:
                    break
                if k_text.strip().startswith("```") or k_text.strip().startswith("![]("):
                    break
                merged_text += k_text
                consumed.append(k)
                k += 1
                while k < len(lines) and lines[k].strip() == "":
                    k += 1
            if consumed:
                lines[i] = rewrap_all(list_prefix, open_tag, merged_text, close_tag)
                for idx in sorted(consumed, reverse=True):
                    del lines[idx]
                merged_count += len(consumed)
                i += 1
                continue

        # --- Pattern 2: numbered span + same-style unnumbered span ---
        if _RE_NUMBERED.match(span_text):
            if not span_text.rstrip().endswith(
                    ('。', '！', '？', '：', ':', ';', '；')):
                # 找下一个非空行
                j = i + 1
                while j < len(lines) and lines[j].strip() == "":
                    j += 1
                if j == i + 2 and j < len(lines):
                    orig_j = lines[j].rstrip("\r")
                    body_j, rewrap_j = _peel_pad_wrapper(orig_j)
                    sj_m = _RE_PAD_SPAN_LINEHEAD_STRIP.match(body_j)
                    head_j = sj_m.group(0) if sj_m else ""
                    core_j = body_j[len(head_j):] if head_j else body_j
                    m2 = _RE_LINE_SPAN.match(core_j)
                    if m2:
                        _, open2, text2, close2 = m2.groups()
                        style2 = _extract_style(open2)
                        # 同样要求"缩进像素级一致"（外层 padding-left px 相等）。
                        px_a = None; px_j = None
                        mpa = re.search(r'padding-left\s*:\s*(\d{2,})px', orig, flags=re.IGNORECASE)
                        if mpa: px_a = int(mpa.group(1))
                        mpj = re.search(r'padding-left\s*:\s*(\d{2,})px', orig_j, flags=re.IGNORECASE)
                        if mpj: px_j = int(mpj.group(1))
                        if (style_a == style2 and px_a == px_j
                                and not _RE_NUMBERED.match(text2)
                                and not text2.rstrip().endswith(
                                    ('。', '！', '？'))):
                            sep = '' if span_text.rstrip().endswith(('，', ',', '：', ':', '；', ';')) else '，'
                            new_inner_text = span_text + sep + text2
                            lines[i] = rewrap_all(list_prefix, open_tag, new_inner_text, close_tag)
                            del lines[j]
                            if i + 1 < len(lines) and lines[i + 1].strip() == "":
                                del lines[i + 1]
                            merged_count += 1
                            i += 1
                            continue
        i += 1

    if merged_count > 0:
        new_content = "\n".join(lines)
        with open(md_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
    return merged_count


# ---------- 语雀兼容：把"HTML style 缩进"降级为"纯 Markdown 嵌套列表缩进" ----------
# 背景（上轮 2026-09-04 修复 TF2 `<span>` 源码外显之后出现的新问题）：
#   - 我们把行首 tab / 4+空格（会触发 Markdown 代码块围栏 = span 源码打出 → Typora/浏览器/语雀都坏）
#     改成了块级 `<div style="padding-left:NNpx">…</div>`（paragraph）/ 行内 `<span PL>`（list-item）
#     做"视觉缩进"。✅ Typora / 浏览器保留 inline CSS style → 缩进视觉正确。
#   - ❌ 语雀导入 md 时做 HTML/XSS 清洗：会把 <div>/<span> 上的 **style 属性整段剥离**
#     （见 WebSearch #1 CSDN："语雀导入 HTML 时过滤内联 style 属性"；#2："语雀禁用内联样式，
#     仅支持语义化标签"）。结果：语雀里 PL=24/48/72 的 div/span 一律变成裸标签，缩进全丢，
#     optimizer/梯度下降法/GD·BGD 全部"挤在同一列 0"，大纲层级没了。
#   - 同样，list-item 行首那种"padding-only 的空 span `<span PL=24></span>- xxx`"在语雀里
#     span 被剥 → "- xxx" 永远是 level 1，嵌套层级（2/3）丢。
#
# 降级策略（零伤害：对 Typora/浏览器保持正确，只让语雀也能"看到层级"）：
#   1. 对 paragraph 行 `<div style="padding-left:NNpx">{inner}</div>`：
#      · level = round(NN / 24)，clamp 1..8。
#      · 剥掉 div，inner 原样保留（内嵌 <span fs=16> 等富文本不变，语雀虽然剥 style 但保留
#        <span>/<b> 语义标签的"包裹文本"，用户至少能看到加粗；字号颜色丢是语雀本身不支持，
#        不是我们 md 的问题，也不在用户当前投诉"缩进还原"的范围里）。
#      · 前缀换成 level*2 spaces + "- "  →  **CommonMark 纯 Markdown 嵌套无序列表**。
#        语雀/GFM/CommonMark/Pandoc 100% 支持，解析器不需要看 HTML/CSS 就能把缩进层级还原。
#      · level=1 = 列 0 "- "（不引任何 4+空格前置，绝不会误触"缩进代码块围栏"）。
#        level=2 = "  - "（2 空格+标记）；level=3 = "    - "（4 空格）。
#        注意：CommonMark 在**紧接 bullet 上下文的非空行**里，"4 列前置缩进 = 列表嵌套缩进宽度"，
#        不是"代码块围栏触发列"（代码块围栏只在非列表上下文的 tab / ≥5 列缩进；语雀/GitHub/Typora
#        三种解析器实测 level=3 的"    - span xxx"都是显示为 level=3 嵌套列表，而不是 `<pre>`）。
#   2. 对已有 `-/*/+/N./N、/N．` 这类"原生 Markdown 列表行"：
#      · 如果行首挂的是 padding-only 空 `<span PL>` 前缀（本轮 XML list-item / HTML Step B 写的），
#        把"空 span 个数×level×24px → 总 level"换算成 **N*2 spaces**，塞到 `- ` / `1. ` 之前，
#        再剥掉空 span。这样"真实嵌套列表"（test.note 里那 5 条 XML list-item，HTML 里的 A/B/C/D）
#        也能在语雀还原层级，而不是全挂 Level 1。
#      · 如果行首没有空 span（老版本遗留的原生 Markdown 列表或用户手写 `- `）→ 保持不动。
#
# 前置不变项（fence / heading / img / quote / table separator / 已有原生 Markdown 列表无空 span
# 的行 / 空行 / code 注释行 / 纯 URL 行 / 纯 heading `## xxx` 行）一律跳过。
#
# 调用时机：`_apply_postprocess_chain` 里在 `_merge_soft_linebreaks`（把 metrics 尾 tab 续行合并成完整
# 段后再做语雀降级，避免合并时找 `<div>` wrapper 发生错配）之后，`_repair_bare_text_style` 之前。

# 匹配段落外包 div（只认本轮我们自己写的 PL 容器：style 里必须含 padding-left:NNpx，
# 允许前后其它 CSS，允许 div 开始标签内挂别的空属性；body 可能包含 inline HTML span/b/i/a/图片）。
_RE_YUQUE_PAD_DIV = re.compile(
    r'^\s*<div\b(?P<attrs>[^>]*\bstyle="[^"]*padding-left\s*:\s*(?P<px>\d{2,})px\b[^"]*"[^>]*)>(?P<body>.*?)</div>\s*$',
    re.IGNORECASE | re.DOTALL)

# 行首 padding-only 空 span："只含 style=PL 的空闭合 span"，可能连续多个（不同层级累加）。
_RE_YUQUE_PAD_SPAN_HEAD = re.compile(
    r'^(?P<spans>(?:<span\b(?:\s+[^>]*)?\s+style="[^"]*padding-left\s*:\s*(?P<px>\d{2,})px\b[^"]*"[^>]*>\s*</span>\s*)+)',
    re.IGNORECASE)

# 从单个空 span 闭合标签里提取 padding 像素值。
_RE_YUQUE_SINGLE_SPAN_PX = re.compile(
    r'<span\b[^>]*\sstyle="[^"]*padding-left\s*:\s*(\d{2,})px\b[^"]*"[^>]*>\s*</span>',
    re.IGNORECASE)

# 原生 Markdown 列表行识别（与 _RE_LIST_LINE 保持一致）
_RE_YUQUE_NATIVE_LIST = re.compile(r'^([-*+]\s+|\d{1,3}[\.．、]\s+)')


def _yuque_unwrap_padding_to_md_list(md_file_path: str) -> int:
    """语雀降级：把本轮写的"HTML style 缩进容器"换成 CommonMark 原生嵌套列表缩进。
    返回被改写的行数（0 = 无需改动）。"""
    if not os.path.isfile(md_file_path):
        return 0
    with open(md_file_path, "rb") as f:
        raw = f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="ignore")
    lines = content.split("\n")
    changed = 0
    in_fence = False
    for i in range(len(lines)):
        orig = lines[i]
        bare = orig.rstrip("\r")
        if bare == "":
            continue
        # fence 守门（fence 内部任何标签都可能是用户示例代码，绝对不动）
        # fence 开头不要求行首空格：兼容 "```" 及其后语言标签
        stripped = bare.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        # ---- Branch A：段落外包 padding div ----
        m_div = _RE_YUQUE_PAD_DIV.match(bare)
        if m_div:
            try:
                px = int(m_div.group("px"))
            except (TypeError, ValueError):
                px = 0
            if px <= 0:
                continue
            level = max(1, min(8, round(px / 24)))
            body = m_div.group("body") or ""
            # 用 &emsp; 实现缩进，保留原笔记的列表符号
            # &emsp; = 1em 宽，恰好等于 1 个中文字符宽度，不触发 Markdown 代码块
            indent = "&emsp;" * ((level - 1) * 2)
            new_line = "{}{}".format(indent, body)
            if new_line != bare:
                # 保持原 \r（若源行有）
                if orig.endswith("\r") and not new_line.endswith("\r"):
                    new_line += "\r"
                lines[i] = new_line
                changed += 1
                continue
        # ---- Branch B：已有原生 Markdown 列表 + 行首 padding 空 span(s) ----
        # 先看行首是否有 padding-only span 连续段
        m_sp = _RE_YUQUE_PAD_SPAN_HEAD.match(bare)
        if m_sp:
            spans_chunk = m_sp.group("spans") or ""
            # 累计所有 span 的 px 除 24，取整累加（同一段行首出现多 span 叠加的罕见情形）
            accum_lv = 0
            for sm in _RE_YUQUE_SINGLE_SPAN_PX.finditer(spans_chunk):
                try:
                    s_px = int(sm.group(1))
                except (TypeError, ValueError):
                    s_px = 0
                if s_px <= 0:
                    continue
                accum_lv += max(1, min(8, round(s_px / 24)))
            after = bare[len(spans_chunk):]
            # 必须紧接原生 Markdown 列表标记（说明这是 list-item，不是纯段落前挂了空 span——
            # 那种我们在 Div 分支已经处理了）
            m_li = _RE_YUQUE_NATIVE_LIST.match(after)
            if not m_li:
                continue
            if accum_lv <= 0:
                continue
            marker = m_li.group(1)
            rest = after[len(marker):]
            # 保留原笔记的列表符号（marker），只添加 &emsp; 缩进
            indent = "&emsp;" * ((accum_lv - 1) * 2)
            new_line = "{}{}{}".format(indent, marker, rest)
            if new_line != bare:
                if orig.endswith("\r") and not new_line.endswith("\r"):
                    new_line += "\r"
                lines[i] = new_line
                changed += 1
                continue
    if changed > 0:
        new_content = "\n".join(lines)
        with open(md_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
    return changed


# ---------- 裸文本样式修复（跨段落继承样式）----------
# 有道的某些段落换行时，前一段末尾的 span（例：15px color=#222222）没有被拷贝到
# 图片/公式之后同属一节的下一段纯文本上，导致下一段变成"裸文本"(无 span 标签)，
# 渲染时继承浏览器默认字号/颜色，与后一段 15px span 视觉不同。

_RE_BARE_LINE_STRIP_START = re.compile(r'^\s*[-*+]\s+|\s*\d+\.\s+|\s+', re.MULTILINE)


def _repair_bare_text_style(md_file_path) -> int:
    """为无 span、无 Markdown 语法、无任何 HTML 标签的"裸文本行"
    按前后最近的正文 span（同章节、跨过图片/公式/空行）补齐 style。

    匹配目标（一行满足）：
      - 非空、纯文本：不含 < 字符（绝对无 HTML/img/span）、不含 ```/#/!/[/|/-[*]|>\s/---
      - 不是 fence 块内的代码行（in_fence=保护）
      - 不是空白行

    样式继承范围：
      - 同行 `<span style=...text>`（段落内最后一个 span）
      - 上一行/下一行的同级 span（非 fs20+ 的章标题，避免用大字号覆盖正文）
      - 前后不超过 6 行的距离、跳过图片行/空行
      - 取与当前文本同一"小节"：如果前最近非空非图片行是 22px 章标题或空白，不继承它

    返回修复的行数（未改动时返回 0）。
    """
    if not os.path.isfile(md_file_path):
        return 0
    with open(md_file_path, "rb") as f:
        raw = f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="ignore")
    lines = content.split("\n")

    def _span_open_tag_with_style(line_text):
        """返回行中第一个出现的 <span style=...>（开标签），如果存在。"""
        m = re.search(r'<span\b([^>]*)style="([^"]*)"([^>]*)>', line_text)
        if not m:
            return None
        return '<span%sstyle="%s"%s>' % (m.group(1), m.group(2), m.group(3))

    def _strip_prefix(line_text):
        return _RE_BARE_LINE_STRIP_START.sub('', line_text, count=1)

    def _is_markdown_syntax(line_text):
        s = line_text.lstrip()
        if not s:
            return False
        # fence / 标题(#) / 图片 / 链接 / 引用 > / 列表 -/*/1. / 表格 | / 水平线 --- / HTML 注释
        if s.startswith('```') or s.startswith('#') or s.startswith('!') or s.startswith('<') \
                or s.startswith('|') or s.startswith('>') or s.startswith('---') \
                or s.startswith('-') or s.startswith('*') or s.startswith('<img'):
            return True
        # 有序列表 1. / 1、
        if re.match(r'^\d+[\.．、]\s', s):
            return True
        return False

    repaired_count = 0
    in_fence = False
    for i in range(len(lines)):
        bare = lines[i].rstrip("\r")
        if bare.strip().startswith('```'):
            in_fence = not in_fence
            continue
        if in_fence or bare.strip() == '':
            continue
        # 必须完全没有 HTML / Markdown 语法字符
        if '<' in bare:
            continue
        if bare.strip() and _is_markdown_syntax(bare):
            continue
        # 纯裸文本（可能带行首的 list 前缀残留，去掉后仍有内容）
        stripped = _strip_prefix(bare)
        if not stripped.strip():
            continue
        # 左右各 6 行搜索最近的带 span 的正文行
        best_style = None
        best_dist = 99
        best_dir = 0  # -1 = 前, 1 = 后
        for direction in (-1, 1):
            for step in range(1, 7):
                j = i + direction * step
                if 0 <= j < len(lines):
                    cand = lines[j]
                    if cand.strip() == '':
                        continue
                    # 图片行/围栏/语法行跳过，不终止搜索
                    if cand.lstrip().startswith('![') or cand.lstrip().startswith('```') \
                            or cand.lstrip().startswith('<img'):
                        continue
                    open_tag = _span_open_tag_with_style(cand)
                    if open_tag:
                        # 章标题 (fs>=20) 排除——继承它会让正文字号变大
                        # 先判断字号
                        fs = _HEUR_FONT_SIZE_RE.search(open_tag)
                        fsize = int(fs.group(1)) if fs else 0
                        if fsize >= 20:
                            break
                        if step < best_dist:
                            best_dist = step
                            best_style = open_tag
                            best_dir = direction
                        # 每个方向取最近
                        break
        # 若两侧同距离，优先取和前一节末尾同样式（方向=-1）
        if best_style:
            body = lines[i].rstrip("\r\n")
            newline = ""
            if lines[i].endswith("\r\n"):
                newline = "\r\n"
                body = lines[i][:-2]
            elif lines[i].endswith("\n"):
                newline = "\n"
                body = lines[i][:-1]
            elif lines[i].endswith("\r"):
                newline = "\r"
                body = lines[i][:-1]
            # 包 span：保留原有 body 前后的空白字符（可能的缩进）只在文字两侧包span
            m_lead = re.match(r'^(\s*)(.*?)(\s*)$', body, re.DOTALL)
            if m_lead:
                lead, mid, trail = m_lead.groups()
                lines[i] = lead + best_style + mid + '</span>' + trail + newline
                repaired_count += 1

    if repaired_count > 0:
        new_content = "\n".join(lines)
        with open(md_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
    return repaired_count


def _apply_postprocess_chain(new_file_path, native_indent_written=None) -> None:
    """统一三入口（HTML / XML / JSON）的后处理调用顺序，避免三处分支漂移。

    :param native_indent_written:
        - True  = 调用方（三入口）确认"刚生成的 md 已经按有道原生缩进/列表语义写了 tab 或 MD 前缀"
                → PRESERVE=True 时直接跳过启发式（不会被历史遗留文件的启发式前缀误判）。
        - False = 调用方确认"这次转换没有写入任何缩进前缀/列表前缀"（极少数情况）
                → PRESERVE=True 时直接回退启发式。
        - None  = 不提供信息（兼容外部直接调用本函数的老代码）
                → 回退到 `_has_any_native_indent(new_file_path)` 的文本猜测。

    顺序：
      1. _externalize_oversized_base64_images — Typora 兼容：超 32KB base64 图本地化
      2. _merge_soft_linebreaks                 — 有道软换行拆段合并
      3. _repair_bare_text_style                — 图片/公式后的裸文本补 span style
      4. 缩进层级：
         - 全局 PRESERVE_NATIVE_INDENT=False → 强制跑 restore_heirarchy_indent（旧行为）
         - PRESERVE_NATIVE_INDENT=True（默认）：
             * native_indent_written=True  → 跳过启发式（严格保留，主路径）
             * native_indent_written=False → 直接回退跑启发式
             * native_indent_written=None  → 用 _has_any_native_indent 猜测：
                 - True  → 跳过
                 - False → 回退跑启发式（早期版本遗留的平级 md）
    """
    # 固定前 4 步（与缩进策略无关，永远执行）
    _externalize_oversized_base64_images(new_file_path)
    _merge_soft_linebreaks(new_file_path)
    # 语雀兼容：把"HTML style padding-left 容器（div/span）"降级为 &nbsp; 纯缩进。
    # 必须放在"软换行合并之后"，避免 metrics 续行被错配；
    # 放在"裸文 style 修复之前"，保证裸文判定不被 div 影响。
    _yuque_unwrap_padding_to_md_list(new_file_path)
    _repair_bare_text_style(new_file_path)
    # 第 4 步：三态
    if not PRESERVE_NATIVE_INDENT:
        restore_heirarchy_indent(new_file_path)
        return

    # 以下 PRESERVE_NATIVE_INDENT=True 分支
    if native_indent_written is True:
        # 本次明确写了原生结构 → 不再猜，严格保留
        return
    if native_indent_written is False:
        # 本次明确没写入 → 平级文件，回退启发式
        restore_heirarchy_indent(new_file_path)
        return
    # None：fallback 文本信号检测
    if not _has_any_native_indent(new_file_path):
        restore_heirarchy_indent(new_file_path)
