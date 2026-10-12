# -*- coding: utf-8 -*-
"""共享样式工具：style 字符串合并、嵌套 span 扁平化、有道颜色值规范化。

XML 与 JSON 两条转换链都会用到，抽出来是为了避免
JsonConvert -> XmlElementConvert 的横向依赖。"""

import re

# ---------- Typora 兼容：合并嵌套 span / 移除空 span ----------


def merge_style_strings(styles):
    """合并多个 CSS style 字符串，同名属性后者覆盖前者，返回合并后的 style 值（不含引号）。
    例如 ["color:red", "font-size:22px", "color:blue"] -> "color:blue;font-size:22px"。"""
    merged = {}
    order = []
    for s in styles:
        if not s:
            continue
        for part in s.split(";"):
            part = part.strip()
            if not part or ":" not in part:
                continue
            k, v = part.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k not in merged:
                order.append(k)
            merged[k] = v
    if not merged:
        return ""
    return ";".join("{}:{}".format(k, merged[k]) for k in order)


def flatten_nested_spans(text):
    """Typora 兼容：移除空 <span>，并将多层嵌套/包裹的 <span style="..."> 扁平化。

    两种合并模式（视觉效果与合并前完全等价，不影响浏览器/语雀）：
      模式 A - 纯嵌套：<span A><span B>T</span></span>
                →  <span A+B>T</span>
      模式 B - 包裹多个兄弟 span：
                <span OUTER>  <span IN1>T1</span>  <span IN2>T2</span>  </span>
                →  <span OUT+IN1>T1</span>  <span OUT+IN2>T2</span>
    扁平化后只有单层 span（或不嵌套的并列 span），Typora 会正常渲染为效果，
    不会再把原始 HTML 标签当作文本显示。"""
    import re
    if not text or "<span" not in text:
        return text

    # 1) 反复移除空 span：<span ...></span>（允许中间仅含空白）
    pat_empty = re.compile(r'<span\b[^>]*>\s*</span\s*>', re.IGNORECASE)
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = pat_empty.sub("", cur)
    text = cur

    # ---------- 工具：从属性串中提取 style 值 ----------
    def _style_of(attrs):
        m = re.search(r'style\s*=\s*"([^"]*)"', attrs, re.IGNORECASE)
        return m.group(1) if m else ""

    # 模式 A：纯嵌套两层 <span A>[空白]<span B>INNER</span>[空白]</span>
    #        INNER 中不得出现任何 <span / </span 标签（保证是最内层两层）
    pat_nested = re.compile(
        r'<span\b([^>]*)>\s*<span\b([^>]*)>((?:(?!</?span\b).)*?)</span\s*>\s*</span\s*>',
        re.IGNORECASE | re.DOTALL,
    )

    def _repl_nested(m):
        outer_style = _style_of(m.group(1).strip())
        inner_style = _style_of(m.group(2).strip())
        inner_text = m.group(3)
        merged = merge_style_strings([outer_style, inner_style])
        if not merged:
            return inner_text
        return '<span style="{}">{}</span>'.format(merged, inner_text)

    # 模式 B：外层 span 的内容 = 空白 + 若干"单层内层 span"交替
    #         把外层 style 合并到每个内层 span，然后移除外层
    #         每个内层 span 的内容中同样不得出现 <span / </span
    _single_span = r'<span\b[^>]*>(?:(?!</?span\b).)*?</span\s*>'
    pat_wrap = re.compile(
        r'<span\b([^>]*)>'                         # 1: 外层属性
        r'(\s*(?:' + _single_span + r'\s*)+)'      # 2: 只含 span + 空白（内层都是单层）
        r'</span\s*>',
        re.IGNORECASE | re.DOTALL,
    )
    # 内层 span 匹配：1=属性，2=内容
    inner_span_pat = re.compile(
        r'<span\b([^>]*)>((?:(?!</?span\b).)*?)</span\s*>',
        re.IGNORECASE | re.DOTALL,
    )

    def _repl_wrap(m):
        outer_style = _style_of(m.group(1).strip())
        inner_block = m.group(2)

        def _each_inner(im):
            inner_style = _style_of(im.group(1).strip())
            inner_text = im.group(2)
            merged = merge_style_strings([outer_style, inner_style])
            if not merged:
                return inner_text
            return '<span style="{}">{}</span>'.format(merged, inner_text)

        return inner_span_pat.sub(_each_inner, inner_block)

    # 2) 交替运行 模式A / 模式B，直到两者都不再变化（因为模式B产物可能触发模式A，反之亦然）
    prev = None
    cur = text
    while prev != cur:
        prev = cur
        cur = pat_nested.sub(_repl_nested, cur)
        cur = pat_wrap.sub(_repl_wrap, cur)
    return cur


def normalize_color_value(value):
    """把有道笔记中各种颜色值规范化为 CSS 可识别的 #RRGGBB。
    支持：
      - '#RRGGBB' / '#RGB' / 'rgb(...)' → 原样
      - 纯数字字符串：
          * ARGB 带符号 32 位 int（如 -16777216 = 0xFF000000 黑, -1 = 0xFFFFFFFF 白）
          * RGB 24 位无符号 int
      - 'RRGGBB'（无 # 前缀的 6 位十六进制）
    若颜色透明（alpha=0 且 value=0）返回 "" 表示跳过。
    解析失败原样返回字符串。
    """
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    lower = s.lower()
    # 已经是合法 CSS 色值前缀的：直接返回
    if lower.startswith("#") or lower.startswith("rgb") or lower.startswith("hsl") \
            or lower in ("transparent", "inherit", "initial", "currentcolor",
                         "black", "white", "red", "green", "blue", "yellow",
                         "gray", "grey"):
        return s
    # 先尝试解析成十进制/十六进制整数：有道内部常用 ARGB 带符号 int（如 -16777216=黑）
    # 放这里是因为像 "16711680" 这种纯数字串也能匹配 8 位 hex 正则，
    # 但它实际表示的是 0xFF0000 整数颜色，所以必须先让整数分支 consume 掉。
    try:
        n = int(s)
    except (ValueError, TypeError):
        n = None
    if n is not None:
        # 当成 32-bit 无符号
        u = n & 0xFFFFFFFF
        a = (u >> 24) & 0xFF
        r = (u >> 16) & 0xFF
        g = (u >> 8) & 0xFF
        b = u & 0xFF
        # n==0 按有道约定是"未设置颜色/透明"，跳过
        if n == 0:
            return ""
        if a == 0:
            # 可能是用户本来就想写 24-bit RGB，把低 24 位当 RGB
            return "#{:02x}{:02x}{:02x}".format(r, g, b)
        return "#{:02x}{:02x}{:02x}".format(r, g, b)
    import re
    # 形如 RRGGBB（6 位十六进制）
    if re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return "#" + s
    if re.fullmatch(r"[0-9a-fA-F]{8}", s):
        # AARRGGBB：若 AA=00 则透明，否则用 RRGGBB 部分
        if s[:2].lower() == "00":
            return ""
        return "#" + s[2:]
    # 最后：非颜色字符串（如 highlight）——原样返回，让上层决定
    return s
