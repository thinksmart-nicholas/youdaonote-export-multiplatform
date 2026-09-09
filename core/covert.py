import base64
import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from typing import Tuple

# ---------------- 导出策略开关 ----------------
# True = 严格保留有道云笔记原生缩进（XML/JSON 的缩进字段、HTML 的 CSS 缩进样式），
#        不再做任何"猜测式"启发式层级重建（restore_heirarchy_indent 自动跳过）。
# False = 旧行为（当原生字段丢失时用冒号/短句/字号猜层级）。
PRESERVE_NATIVE_INDENT = True

MARKDOWN_SUFFIX = ".md"


def _ex_escape_attr(s):
    """
    转义 HTML/URL 属性值中的特殊字符，避免 src/alt 含引号导致 Markdown 渲染错误。
    用于 <img src="..." alt="..."> 标签属性值。
    """
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _md_image(alt, path):
    """
    输出标准 Markdown 图片语法 ![alt](path)。

    语雀在导入 Markdown 时只识别 ![]() 语法，不会把 HTML <img> 标签转换成图片
    对象，因此白板/流程图一律用 Markdown 语法输出 —— 浏览器、Typora、语雀、
    GitHub、VS Code 都能正常渲染，兼容性比原始 HTML 标签更好。

    路径中的空格转成 %20，避免链接被截断；alt 中的方括号转义，防止提前闭合。
    """
    safe_alt = str(alt or "").replace("\r", " ").replace("\n", " ")
    safe_alt = safe_alt.replace("[", "\\[").replace("]", "\\]")
    safe_alt = safe_alt.replace("<", "").replace(">", "")
    safe_path = str(path or "").replace(" ", "%20")
    return "![" + safe_alt + "](" + safe_path + ")"


# Typora 兼容：内联 base64 图片的最大原始图像字节数
# 超过阈值的 base64 图片会被解码后保存到本地 images/ 目录，替换为相对路径引用。
# 阈值与 core/image.py 中 MAX_INLINE_IMAGE_BYTES 保持一致：
# 32 KB 原始图像 ≈ 43,690 字符的 base64，该值在 Typora 中稳定可解析为图片。
_MAX_INLINE_IMAGE_BYTES = 32 * 1024

# data:image/xxx;base64 → 扩展名映射
_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/webp": ".webp",
}

# 匹配 Markdown 图片中的 base64 data-URI：
#   ![alt](data:image/MIME;base64,<b64字符串>)
# 分组 1=alt, 2=MIME, 3=base64 字符串（贪婪匹配到最后一个 )）
_REGEX_BASE64_IMAGE = re.compile(
    r'!\[([^\]]*)\]\(\s*data:image/([a-zA-Z0-9+\-.]+);base64,([A-Za-z0-9+/=\s]+)\s*\)'
)

# ---------- 段落缩进辅助 ----------
# 有道云笔记 JSON "4" 属性 / XML para 元素 attrib 中，段落缩进等级的候选键名
# （不同版本 / 导出接口会使用不同字段，这里列全以提高命中率）
# 注意：以下键是"段落缩进数值"，同名异义的字段在调用端已避开：
#   - level：XML 中 <heading> 的 level 代表 H1~H6，但 <heading> 走 convert_heading_func，
#            不调用本函数；<para>/<list-item> 的 level 是段落缩进（有效）。
#   - li：JSON type='l' 中的 list-id（UID 字符串，不是数值，**排除**）
#   - ll：JSON type='l' 中的 list-level（整数，真实缩进数，**新增**）
_INDENT_LEVEL_KEYS = (
    "indent", "il", "indent_level", "indentLevel",
    "pl", "paragraph_level", "paragraphLevel", "pi",
    "level", "ll", "list_indent", "listIndent",
    "tab_level", "tabLevel", "tabs", "nest",
)
# 每个缩进等级对应的前缀（1 个 tab = 4 空格，兼容 Typora）
_INDENT_PREFIX_PER_LEVEL = "\t"

# HTML 入口里有道常用的 CSS 缩进像素（典型：一级≈24/32px，每级同宽）
# 这里给两种典型步长都做兜底，用最大公约数换算。
_HTML_INDENT_PX_PER_LEVEL = (24, 32, 40, 48)


def _css_indent_to_level(css_text: str) -> int:
    """从 <p>/<div>/<li> 的 style / class 附带 CSS 文本里估算段落缩进等级。
    扫描 padding-left / margin-left / text-indent 三个属性（px/em/pt/%），
    取三者中最大有效缩进，除以有道常用步长（24/32/40/48px）得等级，最大 8。"""
    if not css_text:
        return 0
    best_px = 0.0
    # 匹配：属性名 : 数值 单位
    for m in re.finditer(r'(padding-left|margin-left|text-indent)\s*:\s*(-?\d+(?:\.\d+)?)\s*(px|em|pt|%)?',
                         css_text, flags=re.IGNORECASE):
        _, raw_num, unit = m.groups()
        try:
            val = float(raw_num)
        except ValueError:
            continue
        if val <= 0:
            continue
        # 单位换算到像素（有道 16px 正文，1em≈16px；1pt≈1.33px；% 按 16px≈1% 近似，仅兜底）
        if unit in (None, "px", ""):
            px = val
        elif unit == "em":
            px = val * 16.0
        elif unit == "pt":
            px = val * 1.333
        elif unit == "%":
            px = val * 0.16
        else:
            px = val
        if px > best_px:
            best_px = px
    if best_px <= 0:
        return 0
    best_level = 0
    for step in _HTML_INDENT_PX_PER_LEVEL:
        lv = int(round(best_px / step))
        if lv > best_level:
            best_level = lv
    # 再用最小步长(24)取 floor，避免 36px 这种中间值错判成 0 级
    floor_lv = int(best_px // 24)
    if floor_lv > best_level:
        best_level = floor_lv
    return max(0, min(8, best_level))


def _restore_html_css_indent(html: str) -> str:
    """在调用 markdownify 之前 + 之后做两阶段处理，保证有道云笔记 HTML 中的所有"段落/列表
    层级"信息最终以 Markdown 的行首 tab 形式保留，避免 markdownify 列表处理时剥掉 li
    内部 whitespace 让嵌套缩进丢失。

    处理两类层级来源：
      1. **CSS 缩进属性**：<p>/<div>/<section>/<article>/<blockquote> 上的
         `padding-left / margin-left / text-indent`（px/em/pt/%）→ 按有道典型步长
         （24/32/40/48px）直接把 tab 塞进 HTML 行首（markdownify 对块级非 list 元素
         不剥 whitespace，能保留）。
      2. **<ul>/<ol> 的 DOM 嵌套深度 + <li> 属性**（list-level / data-level / CSS）
         → 不在 HTML 中塞 \t（会被 markdownify 剥），而是在 <li> 开标签上挂一个
         `data-md-indent="<n>"` 属性。markdownify 之后再对 md 输出做一次 post-scan：
         把出现过 `data-md-indent="<n>"` 对应位置的列表行（`- / * / + / 1. / N.`）
         前面补 n 个 tab。
    """
    if not html or PRESERVE_NATIVE_INDENT is False:
        return html

    from html.parser import HTMLParser
    import io

    # ---------- Phase 1 公共辅助 ----------
    # 非 list 块 CSS 缩进占位 marker：必须是"纯文本且 markdownify 不会转义/剥离"，
    # 用 U+200B (zero-width space) + 关键字 做包围，用户原文基本不会出现。
    # Phase 3 按以下正则把它还原成 N 个 tab。
    _TAB_MARKER_PREFIX = "\u200b\u200bMDTAB"  # 后面跟数字 N + "\u200b\u200b"
    _TAB_MARKER_SUFFIX = "\u200b\u200b"

    def _tab_marker(n: int) -> str:
        return "{}{}{}".format(_TAB_MARKER_PREFIX, max(0, min(8, int(n))), _TAB_MARKER_SUFFIX)

    def _line_level_from_attrs(attr_str: str) -> int:
        """从一个标签的 attr 原始字符串（含 style / level / data-level）算 level 增量。"""
        lv = _css_indent_to_level(attr_str)
        if lv <= 0 and attr_str:
            # <li level="2"> / <li data-level="2">
            m1 = re.search(r'(?:^|\s)level\s*=\s*"(\d+)"', attr_str, flags=re.IGNORECASE) \
                 or re.search(r'(?:^|\s)level\s*=\s*(\d+)', attr_str, flags=re.IGNORECASE)
            m2 = re.search(r'(?:^|\s)data-level\s*=\s*"(\d+)"', attr_str, flags=re.IGNORECASE) \
                 or re.search(r'(?:^|\s)data-level\s*=\s*(\d+)', attr_str, flags=re.IGNORECASE)
            c = []
            if m1: c.append(int(m1.group(1)))
            if m2: c.append(int(m2.group(1)))
            if c:
                lv = max(lv, max(0, min(8, max(c) - 1)))
        return lv

    # ---------- Phase 1b：<li> 标记 data-md-indent；非 list 块元素往文本内塞 \t ----------
    class _Mark(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self._out = io.StringIO()
            self._tag_stack = []
            self._li_extras = []   # 每个 li 进入时的属性 level 增量
            self._in_pre = 0
            self._next_li_marker_counter = [0]
            self._md_indent_values = []  # 输出顺序：第 n 个 marker 对应 tabs 值

        def _list_depth(self):
            return sum(1 for t, _ in self._tag_stack if t in ("ul", "ol"))

        def _render_attrs(self, attrs):
            parts = []
            for k, v in attrs or []:
                if v is None: parts.append(k)
                else: parts.append('{}="{}"'.format(k, v.replace('"', "&quot;")))
            return (" " + " ".join(parts)) if parts else ""

        def handle_starttag(self, tag, attrs):
            t = tag.lower()
            attr_str = self._render_attrs(attrs)
            a_lv = 0 if t in ("ul", "ol") else _line_level_from_attrs(attr_str)
            if t in ("pre", "code"):
                self._in_pre += 1; self._tag_stack.append((t, 0))
                self._out.write("<{}{}>".format(tag, attr_str)); return
            if t in ("ul", "ol"):
                self._tag_stack.append((t, 0))
                self._out.write("<{}{}>".format(tag, attr_str)); return
            if t == "li":
                depth = self._list_depth()
                # 列表行缩进 tabs = DOM 嵌套深度 + 自身属性 level 增量
                lv = max(0, min(8, depth + a_lv))
                self._li_extras.append(a_lv)
                self._tag_stack.append((t, a_lv))
                # 在 <li> 上挂 marker 属性（数值用当前 marker 序号，避免重复）
                mid = self._next_li_marker_counter[0]
                self._next_li_marker_counter[0] += 1
                self._md_indent_values.append(lv)
                marker = ' data-md-indent="{}"'.format(mid)
                self._out.write("<li{}{}>".format(attr_str, marker)); return
            # 其他块元素：把 CSS 级缩进"记在栈里"，handle_data 时塞在每行开头
            self._tag_stack.append((t, a_lv))
            self._out.write("<{}{}>".format(tag, attr_str))

        def handle_endtag(self, tag):
            t = tag.lower()
            self._out.write("</{}>".format(tag))
            if self._tag_stack:
                top_t, _ = self._tag_stack[-1]
                if top_t == t: self._tag_stack.pop()
            if t in ("pre", "code"):
                self._in_pre = max(0, self._in_pre - 1)
            elif t == "li" and self._li_extras:
                self._li_extras.pop()

        def handle_startendtag(self, tag, attrs):
            self._out.write("<{}{} />".format(tag, self._render_attrs(attrs)))

        def handle_data(self, data):
            if self._in_pre <= 0 and data:
                # 非 list 场景：取最近块元素栈 a_lv 做行首 \t
                attr_lv = 0
                for tag_name, a_lv in reversed(self._tag_stack):
                    if tag_name in ("ul", "ol", "li", "pre", "code"): continue
                    if a_lv: attr_lv = a_lv; break
                in_li = any(t == "li" for t, _ in self._tag_stack)
                # 在 li 内：已经通过 data-md-indent 在 md 结果后处理补 tab，这里不重复塞
                if in_li:
                    pass
                elif attr_lv > 0:
                    # 关键：markdownify 在非 list 块（<p>/<div>）里会剥掉行首的 \t 和空 inline HTML。
                    # 这里不直接写 \t，而是用"纯文本 Unicode marker"占位，
                    # Phase 3 再把它还原成真实 tab。
                    marker = _tab_marker(attr_lv)
                    parts = data.splitlines(True)
                    data = "".join(
                        (marker + p) if p.strip("\r\n") != "" else p for p in parts
                    )
            self._out.write(data)

        def handle_entityref(self, name): self._out.write("&{};".format(name))
        def handle_charref(self, name): self._out.write("&#{};".format(name))
        def unknown_decl(self, data): self._out.write("<!" + data + ">")
        def handle_comment(self, data): self._out.write("<!--{}-->".format(data))
        def handle_decl(self, decl): self._out.write("<!{}>".format(decl))
        def handle_pi(self, data): self._out.write("<?{}>".format(data))

        def result(self):
            return self._out.getvalue(), self._md_indent_values

    p = _Mark()
    try:
        p.feed(html); p.close()
        marked_html, md_indent_values = p.result()
    except Exception:
        return html

    # ---------- Phase 2：CSS pass ----------
    TAG_RE = re.compile(
        r'<(p|div|section|article|blockquote|pre)\b([^>]*)>(.*?)</\1>',
        flags=re.IGNORECASE | re.DOTALL
    )
    def _css_sub(m):
        tag, attrs, body = m.group(1), m.group(2), m.group(3)
        if tag.lower() == "pre": return m.group(0)
        lv = _css_indent_to_level(attrs)
        if lv <= 0: return m.group(0)
        marker = _tab_marker(lv)
        def _add(s):
            if s.strip() == "": return s
            if _TAB_MARKER_PREFIX in s: return s  # Phase 1 已加，不重复
            return marker + s
        nb = re.sub(r'([^\n]+)', lambda mm: _add(mm.group(0)), body)
        return "<{}{}>{}</{}>".format(tag, attrs, nb, tag)
    for _ in range(3):
        nxt = TAG_RE.sub(_css_sub, marked_html)
        if nxt == marked_html: break
        marked_html = nxt

    # ---------- 把 indent_values 绑定到 Phase 3 的 md 扫描 ----------
    # 挂到闭包：HTMLParser 结束后我们需要把 md_indent_values 传到 Phase 3。
    # 用 function attribute 挂在 _restore_html_css_indent 本身上，方便 markdownify 之后
    # 的调用者通过 _restore_html_css_indent._md_pending 取出。（无状态依赖，无全局污染其他调用）
    setattr(_restore_html_css_indent, "_md_pending", md_indent_values)
    return marked_html


def _apply_md_indent_markers(md_text: str) -> str:
    """配合 `_restore_html_css_indent` 的 Phase 3：
    1. 把"非 list CSS 缩进占位 marker"（纯文本 `\u200b\u200bMDTAB{N}\u200b\u200b`）
       还原成 N 个 tab；同一行相邻多个 marker 累加。
    2. 按 Phase 1 `<li>` 进入顺序，对 markdownify 输出的列表行（`-/*/+/N./N、`）补对应 tabs，
       避免 markdownify 处理 <li> 时把行首 whitespace 剥离导致原生嵌套缩进丢失。

    调用顺序：`_restore_html_css_indent` 必须先调用过，其 `_md_pending` 属性存列表缩进量。"""
    if not md_text:
        return md_text
    # Step A: 纯文本 marker -> N 个 tab（同一行的多段 marker 合并：相邻时重复匹配即累加）
    _TAB_MARKER_RE = re.compile(r"\u200b\u200bMDTAB(\d)\u200b\u200b")
    def _tabmarker(m):
        try: n = int(m.group(1))
        except Exception: n = 0
        if n <= 0: return ""
        # 每个 MDTAB{N} 替换为 N 级"行内软缩进 span"（padding-left N*24px）。
        # 绝对不能写 \t / 4+空格 前缀 —— Markdown 围栏代码块会吞，让 span/font 源码打印。
        return '<span style="padding-left:{}px"></span>'.format(n * 24)
    # 最多扫 4 轮（处理嵌套块级元素同一段落被重复叠加 marker 等罕见情况）
    for _ in range(4):
        if not _TAB_MARKER_RE.search(md_text): break
        md_text = _TAB_MARKER_RE.sub(_tabmarker, md_text)
    md_pending = getattr(_restore_html_css_indent, "_md_pending", None)
    if not md_pending:
        return md_text
    lines = md_text.split("\n")
    marker_seen = 0
    # Phase A：找 data-md-indent="N" 行，它在 markdownify 后会和文本在同一行
    #（因为 marker 放在 <li data-md-indent="N">text</li> → markdownify 输出 'marker + bullet + text'）。
    # 但 markdownify 不保留未知属性在输出里，所以我们需要另一种机制：把 Phase 1 的 data-md-indent
    # 值与输出 Markdown 列表项做"按顺序一一对应"。
    # 因为 Phase 1 在每个 <li> starttag 上都 push 了一个值，md_indent_values 的顺序就是
    # 文档中 <li> 出现的顺序。markdownify 最终输出列表项"顺序" = 文档 <li> 顺序。
    list_line_idx = 0
    _RE_LIST_LINE = re.compile(r'^(\s*)([-*+]\s+|\d{1,3}[\.．、]\s+)')
    for i in range(len(lines)):
        line = lines[i]
        m = _RE_LIST_LINE.match(line)
        if not m:
            continue
        if list_line_idx < len(md_pending):
            tabs = md_pending[list_line_idx]
            list_line_idx += 1
            if tabs > 0:
                # 目标总视觉缩进 = tabs*24 px。
                # markdownify 已经在行首塞了若干 \t（不算 span 里的 padding）：
                #   → 对应像素 = existing_tabs * 24
                # 差额像素统一塞进 span padding-left，避免再写 \t（≥4列 = Markdown 代码块）。
                # 为了让 span 像素直接反映"目标总 level"（便于后续 _has_any_native_indent/
                # 校验正则只看一个 padding 就够），这里把 existing_tabs * 24 也并入差额：
                # 最终行首结构 = <span style="padding-left:tabs*24px"></span> + 原有 ws 前缀 + bullet
                # （原有 ws 前缀不会再额外贡献视觉缩进，只保留 markdownify 原始上下文。）
                existing_tabs = m.group(1).count("\t")
                need = max(0, tabs - existing_tabs)
                if need > 0:
                    pad_px = tabs * 24
                    pad = '<span style="padding-left:{}px"></span>'.format(pad_px)
                    lines[i] = pad + line
    # 清理属性避免串下一次调用
    try:
        delattr(_restore_html_css_indent, "_md_pending")
    except AttributeError:
        pass
    return "\n".join(lines)




def _extract_indent_level(props) -> int:
    """从 JSON content["4"] 属性 dict 或 XML element.attrib / XML Element 子树中提取段落缩进等级。
    支持三种入参：
      - dict：JSON 段落属性 / XML attrib
      - XML Element：递归进入 `<styles>` / `<inline-styles>` 子，找
        `<text-indent> / <indent> / <paragraph-level> / <list-level> / <level>` 的直接文本数值。
    找不到或值非法返回 0（无缩进）。等级最小为 0，最大限制为 8（防御异常值）。"""
    # 入参二：XML Element 对象（不是 dict）
    if props is None:
        return 0
    # 入参二：XML Element 对象（xml.etree.ElementTree 的 Element 没有 `__iter__`，
    # 但 `list(el)` 可通过 __getitem__ 迭代，所以这里用"有 str 型 tag + 有 attrib 映射"双条件判断。）
    _tag = getattr(props, "tag", None)
    _attr = getattr(props, "attrib", None)
    if isinstance(_tag, str) and isinstance(_attr, dict):
        el = props
        # 先读 element.attrib（老格式保留）
        attr_lv = _extract_indent_level(_attr or {})
        if attr_lv > 0:
            return attr_lv
        # 再进 <styles> / <inline-styles> 子查数值标签
        NS_STRIP_RE = re.compile(r'^\{[^}]*\}')
        STYLE_CONTAINERS = ("styles", "inline-styles")
        LEVEL_CHILD_TAGS = ("text-indent", "indent",
                            "paragraph-level", "paragraphlevel", "paragraph_level",
                            "list-level", "listlevel", "list_indent",
                            "level", "tab-level", "tablevel")
        best_xml_lv = 0
        try:
            kids = list(el)
        except Exception:
            kids = []
        for direct_ch in kids:
            try:
                raw_tag = direct_ch.tag
            except Exception:
                continue
            if not isinstance(raw_tag, str):
                continue
            dc_tag = NS_STRIP_RE.sub("", raw_tag).lower().replace("_", "-")
            if dc_tag not in STYLE_CONTAINERS:
                continue
            for sub in list(direct_ch):
                try:
                    sraw = sub.tag
                except Exception:
                    continue
                if not isinstance(sraw, str):
                    continue
                st = NS_STRIP_RE.sub("", sraw).lower().replace("_", "-")
                if st not in LEVEL_CHILD_TAGS:
                    continue
                raw_val = (getattr(sub, "text", None) or "").strip()
                if not raw_val or raw_val in ("0",):
                    continue
                try:
                    v = int(raw_val)
                except (TypeError, ValueError):
                    continue
                if v > best_xml_lv:
                    best_xml_lv = v
        return max(0, min(8, best_xml_lv))
    if not isinstance(props, dict) or not props:
        return 0
    for key in _INDENT_LEVEL_KEYS:
        if key in props and props[key] not in (None, "", 0, "0"):
            raw = props[key]
            try:
                v = int(raw)
            except (TypeError, ValueError):
                # 也可能是 "24px" / "2em" 这类长度值：除以"一级缩进"的典型像素 24/32
                if isinstance(raw, str):
                    m = re.match(r'\s*(\d+)\s*(?:px|em|pt|%)?\s*$', raw)
                    if m:
                        num = int(m.group(1))
                        # 经验：24~32 px 视为一级（有道常用 24px/级）
                        v = max(1, round(num / 24))
                    else:
                        continue
                else:
                    continue
            if v <= 0:
                continue
            return min(v, 8)
    return 0


def _apply_indent_prefix(text: str, level: int) -> str:
    """给段落文本按等级加"不触发 Markdown 代码块"的缩进。
    ⚠️ 不能写行首的 tab/4+空格（会被 Markdown 当代码块，HTML span 原样显示源码）。
    策略：对每一个非空行段包 `<div style="padding-left:N*24px">行段</div>`，让 Typora/语雀/GitHub 渲染器用 CSS 做视觉缩进，同时保留行内 HTML（<span>/<b>/<a>）被渲染。"""
    if level <= 0 or not text:
        return text
    nl_pat = re.compile(r'(\r\n|\r|\n)')
    parts = nl_pat.split(text)
    safe_px = str(level * 24)
    out = []
    for tok in parts:
        if tok in ('\r\n', '\r', '\n'):
            out.append(tok)
            continue
        body = tok.rstrip("\r\n")
        tail = tok[len(body):]
        if body == "":
            out.append(tok)
            continue
        out.append('<div style="padding-left:{}px">{}</div>{}'.format(safe_px, body, tail))
    return "".join(out)


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
# 有序列表条目：1-3 位数字 + 点/顿号 + 非数字（排除小数 "1.5"），点后可带空格
_RE_NUM_ITEM = re.compile(r'^\d{1,3}[\.．、](?!\d)\s*')


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


def _has_any_native_indent(md_file_path) -> bool:
    """双通道开关的辅助：判断 md 文件内是否已经存在"有道原生缩进留下的痕迹"。
    命中以下任一项 => True（视作 PRESERVE=True 模式下已有原生层级，不要跑启发式）：
      1. 非空行以 1+ 个 tab 开头（旧版本 XML/JSON 的 _apply_indent_prefix）
      2. 非空行以 4+ 个空格开头（某些 HTML/编辑器导出示意缩进）
      3. 非空行已是 Markdown 列表项：开头 `- `、`* `、`+ ` 或有序 `N. ` / `N、`
      4. 围栏代码块（```）
      5. **非 list-item 段落的 HTML 缩进容器**：<div style="padding-left:NNpx">（convert_para_func level>0 的新写法，完全避免 tab 触发代码块）
      6. **list-item 的 inline HTML 软缩进**：<span style="padding-left:NNpx"></span>（convert_list_item_func level>0 新写法）
      7. **HTML 入口的非 list CSS 缩进纯文本 marker**：`\u200b\u200bMDTAB{N}\u200b\u200b`（Phase 3 未清理时的存活信号）
      8. **HTML 入口 list marker**：`data-md-indent=`（Phase 1 挂在 `<li>` 上）
      9. **引用 >** / **heading #**：已按 Markdown 语法结构化，都算已处理。
    全文件"平得不能再平"（所有正文 col-0 纯 paragraph / 图片 / 标题）=> False。"""
    if not os.path.isfile(md_file_path):
        return False
    with open(md_file_path, "rb") as f:
        raw = f.read()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        content = raw.decode("utf-8", errors="ignore")
    in_fence = False
    # 预编译：padding-left 数字匹配（para 容器 N*24px / list-item span N*24px）
    # padding-left 在 style=" 里的三种典型位置：
    #   ① 最开头紧跟开引号：<span style="padding-left:24px… → 前面无字符
    #   ② 跟在其他 CSS 之后："; padding-left… / ";padding-left…
    #   ③ 紧跟其他 CSS 无空格分隔：";padding-left…
    # 所以前缀边界 = 非 CSS 标识符连字符的 0~1 字符（不要求强制前导字符）。
    _PAD_PROP = r'padding-left\s*:\s*\d{2,}px\b'
    _BEFORE_PROP = r'(?<![a-zA-Z0-9_-])'   # 负回顾：不能紧贴 CSS 标识字符，避免"xxxpadding-left"误伤
    _STYLE_PREFIX = r'<{tag}\b[^>]*style="'
    _RE_PAD_DIV = re.compile(
        _STYLE_PREFIX.format(tag='div') + r'[^"]*' + _BEFORE_PROP + _PAD_PROP, re.IGNORECASE)
    _RE_PAD_SPAN = re.compile(
        _STYLE_PREFIX.format(tag='span') + r'[^"]*' + _BEFORE_PROP + _PAD_PROP + r'[^>]*>\s*</span>', re.IGNORECASE)
    _RE_PAD_SPAN_LINEHEAD = re.compile(
        r'^(?:' + _STYLE_PREFIX.format(tag='span') + r'[^"]*' + _BEFORE_PROP + _PAD_PROP + r'[^>]*>\s*</span>)+', re.IGNORECASE)
    _RE_HEADING = re.compile(r'^#{1,6}\s+')
    for raw_line in content.splitlines():
        line = raw_line.rstrip("\r")
        stripped = line.lstrip()
        if stripped == "":
            continue
        if stripped.startswith("```"):
            in_fence = not in_fence
            return True  # 存在围栏 → 已处理
        if in_fence:
            continue
        # 0. 先把行首"零宽度 inline padding span"（本轮写的软缩进容器）整块剥掉，
        #    露出后面真实的 \t / 空格 / bullet / > / ## 结构 —— 以下判定据此做。
        headless = _RE_PAD_SPAN_LINEHEAD.sub("", line)
        # 5. div padding-left 容器（TF2 para 缩进新写法） —— 整块出现在哪都行
        if _RE_PAD_DIV.search(line):
            return True
        # 6. inline span padding-left（list-item level>0 新写法 / HTML 入口 MDTAB→span）
        if _RE_PAD_SPAN.search(line):
            return True
        # 1. tab / 2. 4+ 空格 开头（剥掉 padding span 前缀后再看）
        if headless.startswith("\t"):
            return True
        if re.match(r'^( {4,})\S', headless):
            return True
        headless_lstrip = headless.lstrip()
        # 3. Markdown 列表 / 引用 / heading
        if re.match(r'^[-*+]\s+', headless_lstrip):
            return True
        if re.match(r'^\d{1,3}[\.．、]\s+', headless_lstrip):
            return True
        if headless_lstrip.startswith(">"):
            return True
        if _RE_HEADING.match(headless_lstrip):
            return True
        # 7+8. HTML 入口 marker（Phase 3 理论上会清理，但防御性保留）
        if "data-md-indent=" in line:
            return True
        if "\u200b\u200bMDTAB" in line:
            return True
    return False


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


# ---------- 启发式层级缩进恢复（兼容已导出的历史 md）----------
# 当有道笔记段落缩进属性被早期版本 / 其他链路丢失时，通过"字号 + 冒号结尾 + 短条目连续"
# 这三个典型特征，把完全平级的 HTML 行还原为 Markdown 嵌套无序列表结构。
# 例如：fs22 的"model.compile() 三个参数："→ 章标题，fs16 以冒号结尾的"梯度下降法："
# → 父分类，其后的 fs16 短语（<25 字，非冒号结尾）→ 叶子条目，最终输出
# `- xxx:` / `\t- xxx` / `\t\t- xxx` 三层嵌套列表，在浏览器 / 语雀 / Typora 均正确显示。

_HEUR_FONT_SIZE_RE = re.compile(
    r'font-size\s*:\s*(\d+)(?:px|pt|em)?', re.IGNORECASE
)
_HEUR_STRIP_HTML_RE = re.compile(r'<[^>]*>')
_HEUR_PARA_START_RE = re.compile(r'<(p|div|span|h\d|li|ul|ol)\b', re.IGNORECASE)


def _heur_extract_fontsize(line: str) -> int:
    """从行内 HTML 中提取最大的字号（px），找不到返回 0。"""
    sizes = [int(m.group(1)) for m in _HEUR_FONT_SIZE_RE.finditer(line)]
    return max(sizes) if sizes else 0


def _heur_strip_html(line: str) -> str:
    """剥离 HTML 标签，返回纯文本（&nbsp; 等实体暂时保留，只用于长度/结尾判断）。"""
    return _HEUR_STRIP_HTML_RE.sub('', line).strip()


def _heur_colon_ends(s: str) -> bool:
    """文本是否以中文或英文冒号结尾（含结尾标点 + 闭合引号/括号的情况）。"""
    s = s.strip()
    if not s:
        return False
    # 去掉末尾常见的 </span> 之外可能残留的中文标点对、引号对
    tail_pairs = [('“', '”'), ('"', '"'), ('（', '）'), ('(', ')'),
                  ('『', '』'), ('「', '」'), ("'", "'")]
    changed = True
    while changed:
        changed = False
        for op, cl in tail_pairs:
            if s.endswith(cl):
                s = s[:-1]
                changed = True
    return s.endswith('：') or s.endswith(':')


def _heur_is_category(plain: str) -> bool:
    """判断一行是否是"大纲分类行"。
    命中以下任一形态：
      1) 以中文/英文冒号结尾（典型父分类标题：梯度下降法：）
      2) "分类名：解释文本"同一行（冒号在中间：动量优化法：使用动量的梯度下降算法）
         要求冒号左侧 token 长度 1~30 字符（名词短语），冒号右侧还有非空文本。
      3) 短短语分类名（交叉熵损失函数 / SELU / 批归一化 等）：plain 长度 [2, 15] 字，
         只包含中文/英文/数字/空格/小括号（算法/公式/网络名等纯名词短语），
         不含逗号/句号/问号/分号等句读标点。
    提前排除 Markdown 图像/链接语法（plain 内的冒号是语法元字符，不是分类语义）
    """
    s = plain.strip()
    if not s:
        return False
    # 提前排除 Markdown 图像 / 链接 / <img> 语法，以及纯base64字符长串
    if s.startswith("!") or s.startswith("[") or s.lower().startswith("<img"):
        return False
    # === 长解释句/段落类哨兵：形如 "Softmax函数实际上是…例如：" 末尾带例如/如下引导词
    # 开头就是"例如/比如/举例：" → 段内举例引用，绝不是分类
    if re.match(r'^(例如|比如|举例|示例)[：:]', s):
        return False
    # 末尾"例如：/比如：/如下：/如下所示：" + plain≥40字 → 长解释段尾的举例引导，不是分类
    if len(s) >= 40 and re.search(r'(例如|比如|举例|示例|如下|如下所示)[：:]\s*$', s):
        return False
    # 超长完整陈述句（≥70字 且 句号/问号/感叹号结尾）→ 独立解释段落
    if len(s) >= 70 and re.search(r'[。？！.!?]\s*$', s):
        return False
    if _heur_colon_ends(s):
        return True
    # 找第一个冒号（中/英文）
    m = re.search(r'[：:]', s)
    if m:
        left = s[:m.start()].strip()
        right = s[m.end():].strip()
        if left and right and 1 <= len(left) <= 30:
            # 左侧不应以句末标点结尾
            if left[-1] not in '。？！!?.,;；':
                # 收紧：长解释句（整行≥70字且以句号结尾，或冒号右侧是长叙述句）
                # 形如 "其意义在于输出元素是XXX的权重，可以凸显...。例如：" 是解释段落，不是分类
                if len(s) >= 70 and s.rstrip().endswith(('。', '！', '？', '.', '!', '?')):
                    pass
                elif len(right) > 50:
                    pass
                else:
                    return True
    # 规则3：短短语纯名词短语（分类名："交叉熵损失函数""SELU""批归一化"等）
    if 2 <= len(s) <= 15:
        if re.fullmatch(r'[\u4e00-\u9fffA-Za-z0-9\s（）()·\-_/\\]+', s):
            # 排除典型叶子条目：
            if '(' in s or '（' in s:
                return False            # a) 含括号 -> 算法缩写形态的叶子
            if '算法' in s:
                return False            # b) "XXX算法" -> 具体条目
            # c) 纯大写/数字/下划线：>5 字符视为长函数名（叶子）；<=5 允许（SELU/ReLU/GELU）
            if re.fullmatch(r'[A-Z0-9_]+', s) and len(s) > 5:
                return False
            # d) 带下划线 + 字母 且较长：典型 snake_case 函数名
            if '_' in s and len(s) > 8 and any(c.isalpha() for c in s):
                return False
            return True
    return False


def restore_heirarchy_indent(md_file_path) -> int:
    """对已导出的 Markdown 文件做启发式层级恢复。
    读取文件每一行，基于"字号 → 冒号结尾 → 短语条目"三级线索推断嵌套结构，
    把平级段落改写为 Markdown 嵌套无序列表。仅当推断出的层级条目 ≥ 3 时才写回，
    防止对无关文件误改。返回实际被改写的条目数量（改写失败 / 未命中返回 0）。"""
    if not os.path.isfile(md_file_path):
        return 0
    with open(md_file_path, "rb") as f:
        raw = f.read()
    try:
        lines_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        lines_text = raw.decode("utf-8", errors="ignore")

    lines = lines_text.splitlines(keepends=True)
    if len(lines) < 5:
        return 0

    # 预处理：提取每一行的元信息（字号、纯文本、是否空行）
    # 跳过 Markdown 原生列表行、代码块围栏、标题行 (#)，保持它们不变
    meta = []  # list of dict: {is_blank, strip, fontsize, plain, colon_end, len_plain, preserve}
    in_code_block = False
    in_html_block = False  # 粗略：遇到 <table> 等块级元素时跳过
    block_stack_depth = 0
    for line in lines:
        info = {"line": line, "preserve": False}
        bare = line.rstrip("\r\n")
        stripped = bare.strip()
        info["is_blank"] = stripped == ""
        info["strip"] = stripped

        if not stripped:
            meta.append(info)
            continue

        # 代码块标记
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_code_block = not in_code_block
            info["preserve"] = True
            meta.append(info)
            continue
        if in_code_block:
            info["preserve"] = True
            info["in_code"] = True
            meta.append(info)
            continue

        # 原生 Markdown 结构：# 标题、> 引用、-/*/1. 列表、表格分隔、--- 水平线、图片 ![ ]、链接行
        if re.match(r'^\s{0,3}(#{1,6})\s+', stripped):
            info["preserve"] = True
            meta.append(info)
            continue
        if re.match(r'^\s{0,3}[-*+]\s+', stripped) or re.match(r'^\s{0,3}\d+\.\s+', stripped):
            info["preserve"] = True
            meta.append(info)
            continue
        if stripped.startswith(">") or re.match(r'^-{3,}\s*$', stripped) \
                or re.match(r'^\s*\|', stripped) or stripped.startswith("!"):
            info["preserve"] = True
            meta.append(info)
            continue

        # 整行代码样式（有道代码块语法高亮：深色背景 #262626/#20211b 等 + 18px 代码字号），
        # 且整行不含 15/16/22px 等正文样式（正文行里内联的单个代码词如 "patience" 不受影响）。
        # 这种行是代码片段，绝不能加 "\t-" 前缀——Markdown 行首 tab 会被当成"缩进制码块"，
        # 导致整行（含 <span> 源码）在浏览器/Typora 里都按代码原样显示。
        if re.search(r'background(?:-color)?\s*:\s*#(?:262626|20211b|1e1e1e|282c34|2d2d2d)', bare, re.I) \
                and re.search(r'font-size\s*:\s*18px', bare, re.I):
            fs_matches = re.findall(r'font-size\s*:\s*(\d+)px', bare, re.I)
            if fs_matches and all(fs == "18" for fs in fs_matches):
                info["preserve"] = True
                meta.append(info)
                continue

        # 纯文本代码行（无 span 样式的 shell/命令代码，如 tflite_convert 转换命令）。
        # 不依赖样式，按文本特征识别，命中即 preserve（不加 "- "/tab，原样保留）：
        #   a) shell 续行：行尾是 "\"（可带空白）且整行不含中文（中文正文不会以反斜杠结尾）
        #   b) 命令行选项开头：--long-opt 或 -x（"-" 后紧跟字母，与 Markdown 列表 "- " 区分；
        #      负数 "-1" 不命中，要求字母开头）
        if (not re.search(r'[\u4e00-\u9fff]', stripped) and re.search(r'\\\s*$', stripped)) \
                or re.match(r'^-{1,2}[A-Za-z]', stripped):
            info["preserve"] = True
            meta.append(info)
            continue

        # 提取字号和纯文本
        info["fontsize"] = _heur_extract_fontsize(bare)
        plain = _heur_strip_html(bare)
        info["plain"] = plain
        info["len_plain"] = len(plain)
        info["colon_end"] = _heur_colon_ends(plain)
        info["is_category"] = _heur_is_category(plain)
        # 提取第一个冒号左侧 token 长度（用于"同级兄弟分类"判断）
        m_col = re.search(r'[：:]', plain)
        if m_col:
            left_tok = plain[:m_col.start()].strip()
            info["left_len"] = len(left_tok)
        else:
            info["left_len"] = 0

        meta.append(info)

    # 推断层级：使用"父分类栈" [(level, fontsize, top_plain_len)]
    #   规则1：字号 ≥ 20 → 章标题 level = 0（清空栈）。若标题以冒号结尾则入栈 level=0 作为根分类
    #   规则2：普通字号 + is_category → 父分类。先弹出父字号 > 当前字号的跨大段项；
    #          再弹出栈中 level ≥ 新 level 的旧项（替换为同级新兄弟）；
    #          新 level = 栈顶.level + 1
    #   规则3：普通字号 非分类 → 叶子条目 / 独立段落 / 组内后续行
    #          leaf_level = 栈顶.level + 1（无栈则保守=1）；长句 plain>60 字视为独立 para level=0
    #   规则4：多行连续组（同字号、全部非分类、相邻无空行）→ 第一行做列表首项，后续同级缩进（不加 -）

    n = len(meta)
    wanted = [0] * n
    role = [None] * n
    group_first = [False] * n
    group_rest = [False] * n

    # 先分组（组内必须：字号相同、全部非分类、相邻没有空行）
    i = 0
    while i < n:
        if meta[i]["is_blank"] or meta[i].get("preserve") or "plain" not in meta[i]:
            i += 1
            continue
        if meta[i].get("is_category"):
            # 分类行本身不组成多行组
            i += 1
            continue
        fs = meta[i].get("fontsize", 0)
        j = i
        while j + 1 < n and not meta[j + 1]["is_blank"] \
                and not meta[j + 1].get("preserve") and "plain" in meta[j + 1] \
                and meta[j + 1].get("fontsize", 0) == fs \
                and not meta[j + 1].get("is_category"):
            # 组内：下一行必须非分类、字号相同、不空行
            j += 1
        if j > i:
            group_first[i] = True
            for k in range(i + 1, j + 1):
                group_rest[k] = True
            i = j + 1
        else:
            i += 1

    # 主遍历：计算 wanted_level
    category_stack = []  # [(level, fontsize, top_plain_len)]

    for idx, info in enumerate(meta):
        if info["is_blank"] or info.get("preserve") or "plain" not in info:
            continue
        fs = info.get("fontsize", 0)
        plain_len = info.get("len_plain", 0)
        is_cat = info.get("is_category", False)

        if fs >= 20:
            # 规则1：大字号标题 → 清空栈，level=0
            category_stack.clear()
            wanted[idx] = 0
            role[idx] = "title"
            # 若标题本身是分类（以冒号结尾 / 冒号左短语形式），入栈 level=0 作为根
            if is_cat and plain_len < 80:
                left_len = info.get("left_len", 0)
                # 5 元组：(level, fs, plain_len, left_len, is_top_cat)
                category_stack.append((0, fs, plain_len, left_len, False))
            continue

        # 普通字号（14~18）：先弹出"父级字号严格大于当前字号"的跨大段分类
        # 注意：level=0 的章标题作为"锚"不弹出（保证大分类之间可以正确回到同级）
        while category_stack and category_stack[-1][0] >= 1 and category_stack[-1][1] > fs > 0:
            category_stack.pop()

        if is_cat:
            colon_end = info.get("colon_end", False)
            left_len = info.get("left_len", 0)
            # Top-category 判定："optimizer: ... 常用的有：" / "loss: ... 常用的有：" / "metrics: ..."
            # 这种带总结性冒号结尾 + 描述长度较大的分类，是章标题下的一级分类。
            is_top_cat = colon_end and plain_len >= 10
            if not is_top_cat:
                # 兼容 metrics 这种冒号在中间、非常长的行（单行多行组首行形态的一级分类）
                # 如 "metrics:一个列表，用于指定..."。冒号左侧 token ≤ 8 字符且 plain_len >= 25
                if left_len and 1 <= left_len <= 8 and plain_len >= 25:
                    is_top_cat = True
            # 防御：超长完整陈述句（≥70字 + 句末标点）即使 left_len≤8 也不算 top_cat
            # 例："Softmax函数实际上是有限项离散概率分布的梯度对数归一化…例如："
            if is_top_cat and plain_len >= 70 and re.search(r'[。？！.!?][：:]?\s*$', info["strip"]):
                is_top_cat = False

            if is_top_cat:
                # Top-category：回到章 level=0 之下的第一级
                while category_stack and category_stack[-1][0] >= 1:
                    category_stack.pop()

            top_level = category_stack[-1][0] if category_stack else -1

            # 同级兄弟替换：栈中 level ≥1 的前序分类若与当前分类"名字长度量级相当"，视为同级兄弟
            # 限制：top_category（一级父分类）不参与 level=1 的兄弟替换（它是下面分类们的共同父节点）
            #       但 level=1 的"非 top_cat"分类（DenseNet/ResNet 这种）可以被同级兄弟替换
            if not is_top_cat:
                while category_stack:
                    top_lvl, top_fs, top_plen, top_llen, top_istop = category_stack[-1]
                    # 锚章 level=0 不弹
                    if top_lvl <= 0:
                        break
                    # 栈顶是 top_category（一级父），当前分类如果是更深（>=2）或自己不是 level=1 父
                    if top_istop:
                        # top_cat 只接受"自己作为父 + 子分类 top_level+1"的组合
                        # 如果当前分类也要算 top_cat？不，当前分类已确认非 top_cat
                        # → top_cat 作为父保留，不弹
                        break
                    # 名字长度量级相当
                    both_have_colon = (left_len > 0 and top_llen > 0 and left_len <= 15 and top_llen <= 15)
                    both_no_colon = (left_len == 0 and top_llen == 0 and plain_len <= 20 and top_plen <= 20)
                    mixed_short = (
                        (left_len == 0 and plain_len <= 20 and top_llen > 0 and top_llen <= 15) or
                        (top_llen == 0 and top_plen <= 20 and left_len > 0 and left_len <= 15)
                    )
                    if both_have_colon or both_no_colon or mixed_short:
                        category_stack.pop()
                        top_level = category_stack[-1][0] if category_stack else -1
                    else:
                        break

            new_level = top_level + 1
            # 弹出栈中 level ≥ 新 level 的旧分类（替换同层或更深的叔叔分类）
            while category_stack and category_stack[-1][0] >= new_level:
                category_stack.pop()

            wanted[idx] = max(new_level, 1)
            role[idx] = "category"
            category_stack.append((wanted[idx], fs, plain_len, left_len, is_top_cat))
            continue

        # 规则3：非分类行
        if group_rest[idx]:
            k = idx - 1
            while k >= 0 and group_rest[k]:
                k -= 1
            if k >= 0 and (group_first[k] or role[k] in ("leaf", "para")):
                wanted[idx] = wanted[k]
                role[idx] = role[k]
            else:
                wanted[idx] = 0
                role[idx] = "para"
            continue

        if group_first[idx]:
            # 多行组首行：叶子项
            leaf_level = (category_stack[-1][0] + 1) if category_stack else 1
            wanted[idx] = max(leaf_level, 1)
            role[idx] = "leaf"
            continue

        # 孤立行
        plain_stripped = info["plain"].strip()
        # 编号开头的行（1. / 1、 / 1．，点后非数字）：有意的列表条目，强制 leaf，
        # 不受长度/句末标点的 para 判定限制（如 "2.padding='valid'，输出大小 = ..." 较长仍是条目）
        if _RE_NUM_ITEM.match(plain_stripped):
            leaf_level = (category_stack[-1][0] + 1) if category_stack else 1
            wanted[idx] = max(leaf_level, 1)
            role[idx] = "leaf"
            continue
        sentence_end = False
        if plain_stripped:
            # 句末标点：句号/问号/感叹号/括号右 或 句末引号结尾
            tail = plain_stripped.rstrip("\"'”’）)")
            if tail and tail[-1] in '。？！!?':
                sentence_end = True
        if plain_len <= 25 and not sentence_end:
            # 短短语：典型叶子（非完整句子形态，如 "算法名""分类项名"）
            leaf_level = (category_stack[-1][0] + 1) if category_stack else 1
            wanted[idx] = max(leaf_level, 1)
            role[idx] = "leaf"
        elif plain_len < 40 and not sentence_end:
            # 中短长度、非完整句子（"写作："这种短说明 / 短短语条目）→ 仍可作为叶子
            leaf_level = (category_stack[-1][0] + 1) if category_stack else 1
            wanted[idx] = max(leaf_level, 1)
            role[idx] = "leaf"
        else:
            # 完整句子（句号结尾 / 中长句 >=40 字）→ 视为独立段落 level=0
            wanted[idx] = 0
            role[idx] = "para"

    # 补丁：处理"top-category 续行"（如 metrics 一行太长被拆成 3 行的情况）
    # 如果 i 是 is_top_category=True 且 colon_end=False（冒号在中间 形态），
    # 且 i 之后紧跟一个 叶子group（group_first）且 wanted = wanted[i]+1 = level 2 的叶子，
    # 且组中每行plain不长到足以成为独立分类、plain开头没有新主题标记 → 整个组降级为 category 续行
    for idx, info in enumerate(meta):
        if not info.get("is_category"):
            continue
        colon_end = info.get("colon_end", False)
        left_len = info.get("left_len", 0)
        plain_len = info.get("len_plain", 0)
        # top-category 形态：冒号在中间（colon_end=False + left_len > 0）且行很长
        is_top_cat_like = (
            not colon_end and left_len > 0 and left_len <= 8 and plain_len >= 20
            and wanted[idx] == 1
        )
        if not is_top_cat_like:
            continue
        # 从 idx+1 开始跨空行收集"续行链"：
        # 续行 = 非 preserve、非分类、非 fs≥20 标题、开头没有"英文词+冒号"新主题征兆的行。
        # 链遇到新分类行 / 章标题 / preserve 行 / 新主题征兆行即停止（空行可跳过）。
        cat_wanted = wanted[idx]
        chain = []
        k = idx + 1
        while k < n and len(chain) < 6:
            if meta[k]["is_blank"]:
                k += 1
                continue
            m = meta[k]
            if m.get("preserve") or "plain" not in m:
                break
            if m.get("is_category") or m.get("fontsize", 0) >= 20:
                break
            p = m["plain"]
            if not p:
                break
            # 新主题征兆：英文名+冒号开头的新分类行（Tensorboard: / EarlyStopping:）
            if re.match(r'^[A-Z][A-Za-z0-9]*\s*[：:]', p) and len(p) > 15:
                break
            chain.append(k)
            k += 1
        for k2 in chain:
            wanted[k2] = cat_wanted
            role[k2] = "category"
            group_first[k2] = False
            # 续行不加 "- "，只用 cat_wanted 个 tab 对齐到该分类内容列
            group_rest[k2] = True

    # 第二阶段：应用改写
    # level 1 → "- "；level 2 → "\t- "；level 3 → "\t\t- "
    # 编号条目（纯文本以 N. 开头）→ "\t"*(level-1) + "N. "（有序列表，编号吸收进列表
    # 标记，避免 "• 1.xxx" 双重符号）
    # group_rest 行不加列表标记，只加 "\t" * level（对齐到同级列表项内容列）
    rewritten_count = 0
    new_lines = []

    def _in_list_context(seq):
        """seq 中最近的非空行是否处于列表上下文（列表项/列表续行/缩进内容）。"""
        for prev in reversed(seq):
            ps = prev.strip("\r\n")
            if not ps.strip():
                continue
            ps_strip = ps.lstrip()
            if ps.startswith("\t") or re.match(r'[-*+]\s+', ps_strip) \
                    or re.match(r'\d+\.\s+', ps_strip):
                return True
            break
        return False

    for idx, info in enumerate(meta):
        if info["is_blank"]:
            new_lines.append(info["line"])
            continue
        # 图片行：夹在列表上下文中时加 1 个 tab 进列表，
        # 否则 col-0 图片会打断列表上下文，导致其后的嵌套列表项被防护剥级。
        # fence 代码块内的图片示例行（in_code）不处理。
        if info.get("preserve") and not info.get("in_code") \
                and info["line"].lstrip("\r\n").startswith("!["):
            if _in_list_context(new_lines):
                original_img = info["line"]
                newline = ""
                for end in ("\r\n", "\n", "\r"):
                    if original_img.endswith(end):
                        newline = end
                        original_img = original_img[: -len(end)]
                        break
                img_body = original_img.lstrip("\t")
                new_body = "\t" + img_body
                if new_body + newline != info["line"]:
                    rewritten_count += 1
                new_lines.append(new_body + newline)
            else:
                new_lines.append(info["line"])
            continue
        if info.get("preserve") or wanted[idx] <= 0:
            new_lines.append(info["line"])
            continue
        level = wanted[idx]
        # 不要把缩进做到无限深：安全上限 5
        level = min(level, 5)
        original = info["line"]
        # 分离出换行符
        newline = ""
        body = original
        for end in ("\r\n", "\n", "\r"):
            if original.endswith(end):
                newline = end
                body = original[: -len(end)]
                break
        # 去除 body 开头已有的空白（避免叠加）
        stripped_body = body.lstrip()
        if not stripped_body:
            new_lines.append(original)
            continue
        # 编号条目：把纯文本开头的 N. 编号提取为有序列表标记（点统一为半角、后加空格），
        # 编号从 span 文本中移除，避免渲染成 "• 1.xxx" 双重符号
        num_prefix = ""
        plain_for_num = re.sub(r'<[^>]*>', '', stripped_body).strip()
        m_num = re.match(r'^(\d{1,3})[\.．、](?!\d)\s*', plain_for_num)
        if m_num and not group_rest[idx]:
            num_prefix = m_num.group(1) + ". "
            num_literal = m_num.group(0)
            # 从 span 文本开头去掉编号（若有 span 包裹）
            new_body = re.sub(
                r'^(<span\b[^>]*>)\s*' + re.escape(num_literal),
                r'\1',
                stripped_body,
                count=1,
            )
            if new_body == stripped_body:
                # 无 span 包裹的纯文本行：直接剥编号
                new_body = stripped_body[len(num_literal):]
            stripped_body = new_body
        if group_rest[idx]:
            # 组内后续：同级缩进量 = level 个 tab（因为 "- " 占 2 列用 tab 对齐不精确，
            # Markdown 渲染器会自动处理同级缩进一致；这里用 level 个 tab 足以表达层级语义）
            prefix = "\t" * level
        elif num_prefix:
            prefix = "\t" * (level - 1) + num_prefix
        else:
            prefix = "\t" * (level - 1) + "- "
        # 通用防护：行首 tab 在 Markdown 里等于"缩进制码块"（≥4 列缩进）。
        # 只有当前面已有列表上下文（前一非空行是列表项或列表续行）时，tab 才表示嵌套；
        # 否则前导 tab 会让整行（含 <span> 源码）被当成代码块原样显示。此时去掉前导 tab，
        # 退化为列 0 的新列表项，保证渲染安全。
        if prefix.startswith("\t") and not _in_list_context(new_lines):
            prefix = prefix.lstrip("\t")
            if not prefix:
                prefix = "- " if not group_rest[idx] else ""
        new_body = prefix + stripped_body
        rewritten_count += 1
        new_lines.append(new_body + newline)

    # 至少改写 3 行，且改写后内容有变化 → 写回
    if rewritten_count < 3:
        return 0
    new_text = "".join(new_lines)
    if new_text == lines_text:
        return 0
    try:
        with open(md_file_path, "wb") as f:
            f.write(new_text.encode("utf-8"))
    except OSError as err:
        logging.warning("写入启发式缩进恢复后的文件失败：%s（%s）", md_file_path, err)
        return 0
    logging.info("启发式层级缩进恢复：「%s」共改写 %d 行",
                 os.path.basename(md_file_path), rewritten_count)
    return rewritten_count


class XmlElementConvert(object):
    """
    XML Element 转换规则
    """

    @staticmethod
    def convert_para_func(**kwargs):
        """正常文本（粗体、斜体、删除线、链接）
        兼容段落缩进：如果 XML <para> 元素带有段落缩进属性或其 <styles>/<inline-styles>
        下 `<text-indent>` 等数值子元素，在文本前加对应等级的缩进。

        ⚠️ **禁止在行首直接塞 N 个 tab（`\t` ≥ 1 个 = 4 列空格缩进 = Markdown 代码块）**
        —— 一旦触发代码块，文本里的 `<span style>` 会被当成源码原样打印。
        所以对 level>0 用 **HTML padding-left 容器**实现视觉缩进：
          - 如文本里本就含有 `<`（inline HTML，如 <b>/<span>/<a>）→ 包 `<div style="padding-left:N*24px">text</div>`。
            Typora / 语雀 / GitHub 渲染器都会保留 HTML 元素并 inline span 正常渲染，且 div wrapper 不被吞成代码块。
          - 如文本是纯 Markdown 兼容（不含 `<`，无列表/代码/标题语法）→ 仍可以直接 tab 前缀（纯文本 4+空格 在 Typora 下虽会显示成 monospaced 但不是源码；为了和 HTML 分支统一也用 padding-left 更安全）。"""
        text = kwargs.get("text") or ""
        element = kwargs.get("element")
        if element is not None:
            # 优先走 Element 路径（attrib + <styles>/<inline-styles> 子 .text-indent 文本双源）
            level = _extract_indent_level(element)
            if level and text:
                # 避免"行首 tab 触发 Markdown 代码块"：包 HTML padding-left 容器（24px/级 = 有道典型步长）
                body = text.rstrip("\r\n")
                tail = text[len(body):]
                safe_px = str(level * 24)
                body = '<div style="padding-left:{}px">{}</div>'.format(safe_px, body)
                text = body + tail
        return text

    @staticmethod
    def convert_heading_func(**kwargs):
        """标题"""
        level = kwargs.get("element").attrib.get("level", 0)
        level = 1 if level in (["a", "b"]) else level
        text = kwargs.get("text")
        return " ".join(["#" * int(level), text]) if text else text

    @staticmethod
    def convert_image_func(**kwargs):
        """图片"""
        image_url = XmlElementConvert.get_text_by_key(
            list(kwargs.get("element")), "source"
        )
        return "![{text}]({image_url})".format(
            text=kwargs.get("text"), image_url=image_url
        )

    @staticmethod
    def convert_attach_func(**kwargs):
        """附件"""
        element = kwargs.get("element")
        filename = XmlElementConvert.get_text_by_key(list(element), "filename")
        resource_url = XmlElementConvert.get_text_by_key(list(element), "resource")
        return "[{text}]({resource_url})".format(
            text=filename, resource_url=resource_url
        )

    @staticmethod
    def convert_code_func(**kwargs):
        """代码块"""
        language = XmlElementConvert.get_text_by_key(
            list(kwargs.get("element")), "language"
        )
        return "```{language}\r\n{code}```".format(
            language=language, code=kwargs.get("text")
        )

    @staticmethod
    def convert_todo_func(**kwargs):
        """to-do"""
        return "- [ ] {text}".format(text=kwargs.get("text"))

    @staticmethod
    def convert_quote_func(**kwargs):
        """引用"""
        return "> {text}".format(text=kwargs.get("text"))

    @staticmethod
    def convert_horizontal_line_func(**kwargs):
        """分割线"""
        return "---"

    @staticmethod
    def convert_list_item_func(**kwargs):
        """列表：按 XML <list-item> 元素原生缩进等级写缩进。
        用 HTML div padding-left 容器实现视觉缩进，保留原笔记的列表符号。"""
        element = kwargs.get("element")
        list_id = element.attrib["list-id"]
        is_ordered = kwargs.get("list_item", {}).get(list_id)
        text = kwargs.get("text") or ""
        # 保留原笔记的列表符号：有序列表用 "1. "，无序列表用 "- "
        prefix = "1. " if is_ordered else "- "
        line = prefix + text
        # 原生缩进：双源读取 —— ① <list-item level="N"> attrib ② <styles>/<inline-styles> 子 .text-indent 文本
        level = _extract_indent_level(element)
        if level:
            # 用 div padding-left 容器（与 convert_para_func 一致，避免行首 tab 触发代码块）
            body = line.rstrip("\r\n")
            tail = line[len(body):]
            safe_px = str(level * 24)
            body = '<div style="padding-left:{}px">{}</div>'.format(safe_px, body)
            line = body + tail
        return line

    @staticmethod
    def convert_table_func(**kwargs):
        """
        表格转换
        :param kwargs:
        :return:
        """
        element = kwargs.get("element")
        content = XmlElementConvert.get_text_by_key(element, "content")

        table_data_str = f""  # f-string 多行字符串
        nl = "\r\n"  # 考虑 Windows 系统，换行符设为 \r\n
        table_data = json.loads(content)
        table_data_len = len(table_data["widths"])
        table_data_arr = []
        table_data_line = []

        for cells in table_data["cells"]:
            values = cells.get("value")
            if values is None:
                values = ""
            cell_value = XmlElementConvert._encode_string_to_md(values)
            table_data_line.append(cell_value)
            # 攒齐一行放到 table_data_arr 中，并重置 table_data_line
            if len(table_data_line) == table_data_len:
                table_data_arr.append(table_data_line)
                table_data_line = []

        # 如果只有一行，那就给他加一个空白 title 行
        if len(table_data_arr) == 1:
            table_data_arr.insert(0, [ch for ch in (" " * table_data_len)])
            table_data_arr.insert(1, [ch for ch in ("-" * table_data_len)])
        elif len(table_data_arr) > 1:
            table_data_arr.insert(1, [ch for ch in ("-" * table_data_len)])

        for table_line in table_data_arr:
            table_data_str += "|"
            for table_data in table_line:
                table_data_str += f" %s |" % table_data
            table_data_str += f"{nl}"

        return table_data_str

    @staticmethod
    def get_text_by_key(element_children, key="text"):
        """
        获取文本内容
        :return:
        """
        for sub_element in element_children:
            if key in sub_element.tag:
                return sub_element.text if sub_element.text else ""
        return ""

    @staticmethod
    def _encode_string_to_md(original_text):
        """将字符串转义防止 markdown 识别错误"""
        if len(original_text) <= 0 or original_text == " ":
            return original_text

        original_text = original_text.replace("\\", "\\\\")  # \\ 反斜杠
        original_text = original_text.replace("*", "\\*")  # \* 星号
        original_text = original_text.replace("_", "\\_")  # \_ 下划线
        original_text = original_text.replace("#", "\\#")  # \# 井号

        # markdown 中需要转义的字符
        original_text = original_text.replace("&", "&amp;")
        original_text = original_text.replace("<", "&lt;")
        original_text = original_text.replace(">", "&gt;")
        original_text = original_text.replace("“", "&quot;")
        original_text = original_text.replace("‘", "&apos;")

        original_text = original_text.replace("\t", "&emsp;")

        # 换行 <br>
        original_text = original_text.replace("\r\n", "<br>")
        original_text = original_text.replace("\n\r", "<br>")
        original_text = original_text.replace("\r", "<br>")
        original_text = original_text.replace("\n", "<br>")

        return original_text

    # ---------- Typora 兼容：合并嵌套 span / 移除空 span ----------
    @staticmethod
    def _merge_style_strings(styles):
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

    @classmethod
    def _flatten_nested_spans(cls, text):
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
            merged = cls._merge_style_strings([outer_style, inner_style])
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
                merged = cls._merge_style_strings([outer_style, inner_style])
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

    # ---------- XML 富文本样式修复 ----------
    # XML <inline-styles> 中各标签 -> (HTML开标签, HTML闭标签) 或带值的生成器
    _XML_STYLE_PRIORITY = [
        # 数字越小越先开启（即越外层）：span(color) > a(href) > s > b > u > i
        "color", "href", "background", "strike", "bold", "underline", "italic",
        "font-size",
    ]

    # XML 标签名（lower）→ 规范化的内部样式名
    # 有道笔记 XML 里背景色可能叫 highlight / back-color / shading 等
    _XML_STYLE_ALIASES = {
        "strike": "strike",
        "strikethrough": "strike",
        "s": "strike",
        "del": "strike",
        "underline": "underline",
        "underscore": "underline",
        "u": "underline",
        "background": "background",
        "background-color": "background",
        "backcolor": "background",
        "back-color": "background",
        "bgcolor": "background",
        "bg-color": "background",
        "highlight": "background",
        "hl": "background",
        "shading": "background",
        "color": "color",
        "fontcolor": "color",
        "font-color": "color",
        "forecolor": "color",
        "fore-color": "color",
        "fontsize": "font-size",
        "font-size": "font-size",
        "fs": "font-size",
        "bold": "bold",
        "b": "bold",
        "italic": "italic",
        "i": "italic",
        "href": "href",
        "link": "href",
        "url": "href",
    }

    @staticmethod
    def _normalize_color_value(value):
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

    @classmethod
    def _xml_style_tag(cls, style_name, value):
        """返回 (open_tag, close_tag)，例如 ('<b>', '</b>') 或带属性的 a/span"""
        s_raw = style_name.lower().replace("_", "-")
        s = cls._XML_STYLE_ALIASES.get(s_raw, s_raw)
        if s == "bold":
            return ("<b>", "</b>")
        if s == "italic":
            return ("<i>", "</i>")
        if s == "strike":
            return ("<s>", "</s>")
        if s == "underline":
            return ("<u>", "</u>")
        if s == "href":
            safe = value.replace('"', "&quot;") if value else ""
            return ('<a href="{}">'.format(safe), "</a>")
        if s == "color":
            if not value:
                return ("", "")
            norm = cls._normalize_color_value(value)
            if not norm:
                return ("", "")
            safe = norm.replace('"', "&quot;")
            return ('<span style="color:{}">'.format(safe), "</span>")
        if s == "background":
            if not value:
                return ("", "")
            norm = cls._normalize_color_value(value)
            if not norm:
                return ("", "")
            safe = norm.replace('"', "&quot;")
            return ('<span style="background-color:{}">'.format(safe), "</span>")
        if s == "font-size":
            if not value:
                return ("", "")
            safe = str(value).replace('"', "&quot;")
            return ('<span style="font-size:{}px">'.format(safe), "</span>")
        return ("", "")

    @staticmethod
    def _parse_inline_styles(element):
        """解析 element 下的 <inline-styles>，返回 [(from, to, style_name, value, prio), ...]
           未找到则返回空列表"""
        ns_prefix = "{http://note.youdao.com}"
        inline_el = None
        for child in element:
            if child.tag.endswith("}inline-styles") or child.tag == "inline-styles":
                inline_el = child
                break
        if inline_el is None or len(list(inline_el)) == 0:
            return []
        ranges = []
        for style in list(inline_el):
            tag = style.tag.replace(ns_prefix, "")
            fr_el = None
            to_el = None
            val_el = None
            for sub in style:
                sub_tag = sub.tag.replace(ns_prefix, "")
                if sub_tag == "from":
                    fr_el = sub
                elif sub_tag == "to":
                    to_el = sub
                elif sub_tag == "value":
                    val_el = sub
            try:
                fr = int(fr_el.text) if fr_el is not None and fr_el.text else 0
                to = int(to_el.text) if to_el is not None and to_el.text else 0
            except (ValueError, TypeError):
                continue
            if to <= fr:
                continue
            val = val_el.text if val_el is not None else ""
            # 名字归一化：通过 _XML_STYLE_ALIASES 统一为 canonical 名（如 highlight → background）
            norm_raw = tag.lower().replace("_", "-")
            prio_name = XmlElementConvert._XML_STYLE_ALIASES.get(norm_raw, norm_raw)
            if prio_name in XmlElementConvert._XML_STYLE_PRIORITY:
                prio = XmlElementConvert._XML_STYLE_PRIORITY.index(prio_name)
            else:
                prio = 999
            ranges.append((fr, to, prio_name, val, prio))
        # 按 START 位置 + 优先级（外层先）排序
        ranges.sort(key=lambda r: (r[0], r[4]))
        return ranges

    @staticmethod
    def _apply_xml_inline_styles(text, element):
        """根据 element 的 inline-styles，把字符区间的样式用 HTML 标签应用到 text 上。
           无 inline-styles 直接返回原文本。空文本直接返回空字符串（避免空标签）。"""
        if not text:
            return ""
        ranges = XmlElementConvert._parse_inline_styles(element)
        if not ranges:
            return text

        # 每个样式先按当前文本长度裁剪 from/to，得到各自的有效起止位置
        # events: (pos, kind, ridx, prio, pair_eff_to)
        #   kind: 0 = START, 1 = END
        #   pair_eff_to: START 事件携带自身裁剪后的 END 位置（END 事件忽略此字段）
        #   同位置排序: END(1) 在前 START(0) 在后
        events = []
        effective_tos = {}  # ridx -> 裁剪后的结束位置
        for idx, r in enumerate(ranges):
            fr, to, sname, val, prio = r
            if fr >= len(text) or to <= 0:
                continue
            if fr < 0:
                fr = 0
            if to > len(text):
                to = len(text)
            if to <= fr:
                continue
            effective_tos[idx] = to
            events.append((fr, 0, idx, prio, to))   # START: 携带 eff_to 备用
            events.append((to, 1, idx, prio, 0))    # END: pair_eff_to 不用

        events.sort(key=lambda e: (e[0], e[1]))

        # active: [(ridx, open_tag, close_tag, end_pos)]
        active = []
        out_chars = []
        text_len = len(text)
        ev_idx = 0
        for i in range(text_len + 1):
            while ev_idx < len(events) and events[ev_idx][0] == i:
                pos, kind, ridx, prio, pair_eff_to = events[ev_idx]
                _fr, _to_raw, sname, val, _rprio = ranges[ridx]
                if kind == 0:
                    # START: 使用 pair_eff_to（裁剪后）作为结束位置，而非 ranges 原始值
                    ot, ct = XmlElementConvert._xml_style_tag(sname, val)
                    if ot or ct:
                        active.append((ridx, ot, ct, pair_eff_to))
                        out_chars.append(ot)
                else:
                    # END
                    found_at = None
                    for k in range(len(active) - 1, -1, -1):
                        if active[k][0] == ridx:
                            found_at = k
                            break
                    if found_at is not None:
                        reopen = []
                        while len(active) > found_at:
                            k_idx, k_ot, k_ct, k_end = active.pop()
                            out_chars.append(k_ct)
                            # 重开条件：不是目标样式 && 真正还有效（k_end > 当前位置）
                            if k_idx != ridx and k_end > i:
                                reopen.append((k_idx, k_ot, k_ct, k_end))
                        for item in reversed(reopen):
                            active.append(item)
                            out_chars.append(item[1])
                ev_idx += 1
            if i < text_len:
                out_chars.append(text[i])

        while active:
            _, _k_ot, k_ct, _k_end = active.pop()
            out_chars.append(k_ct)

        return "".join(out_chars)

    @staticmethod
    def get_styled_text(element):
        """从 XML 元素提取 text，并在其上应用 inline-styles（富文本）。
           替代原先只取纯文本的 get_text_by_key。
           最后做一次 span 扁平化，兼容 Typora（浏览器/语雀中效果不变）。"""
        text = XmlElementConvert.get_text_by_key(list(element))
        styled = XmlElementConvert._apply_xml_inline_styles(text, element)
        return XmlElementConvert._flatten_nested_spans(styled)


class JsonConvert(object):
    """
    json 转换规则
    """

    def _get_common_text(self, content: dict) -> Tuple[list, str]:
        """获取通常文本
        :return
            text(text): 文本内容
        """
        all_text = ""
        # 5 内容：遍历全部 five_contents（修复：之前只取 five_contents[0] 导致非首块内容丢失）
        five_contents = content.get("5")
        if not five_contents:
            return all_text
        for five_content in five_contents:
            # 如果该块还有内嵌 5（两层嵌套），先递归处理
            inner_five = five_content.get("5")
            if inner_five:
                all_text += self._get_common_text(five_content)
            # 处理本层的 7：文本片段数组
            seven_contents = five_content.get("7")
            if not seven_contents:
                continue
            for seven_content in seven_contents:
                # 8 文本
                text = seven_content.get("8") or ""
                # 9 文本属性
                text_attrs = seven_content.get("9")
                if text and text_attrs:
                    text = self._convert_text_attribute(text, text_attrs)
                all_text += text
        # 做一次 span 扁平化，兼容 Typora（浏览器/语雀中效果不变）
        return XmlElementConvert._flatten_nested_spans(all_text)

    def _convert_text_attribute(self, text: str, text_attrs: list):
        """文本属性：扩展支持删除线、下划线、颜色、背景色等"""
        if not (isinstance(text_attrs, list) and text_attrs and text):
            return text
        for attr in text_attrs:
            if not isinstance(attr, dict):
                continue
            kind = attr.get("2")
            val = attr.get("3")
            if kind == "b":
                # 粗体
                text = "**{}**".format(text)
            elif kind == "i":
                # 斜体
                text = "*{}*".format(text)
            elif kind in ("s", "strike", "strikethrough", "del"):
                # 删除线
                text = "~~{}~~".format(text)
            elif kind in ("u", "underline", "underscore"):
                # 下划线：Markdown 无原生语法，使用 HTML <u>
                text = "<u>{}</u>".format(text)
            elif kind in ("c", "color", "fontColor"):
                # 颜色：使用 HTML span
                if val:
                    norm = XmlElementConvert._normalize_color_value(val)
                    if norm:
                        safe = norm.replace('"', "&quot;")
                        text = '<span style="color:{}">{}</span>'.format(safe, text)
            elif kind in ("bg", "background", "backgroundColor", "hl", "highlight",
                          "backcolor", "backColor", "bgcolor", "bgColor", "shading"):
                # 背景色/高亮（别名覆盖：有道 JSON 内部用多种键）
                if val:
                    norm = XmlElementConvert._normalize_color_value(val)
                    if norm:
                        safe = norm.replace('"', "&quot;")
                        text = '<span style="background-color:{}">{}</span>'.format(safe, text)
            elif kind in ("fs", "fontSize", "font-size"):
                # 字号：HTML span（可选）
                if val:
                    safe = str(val).replace('"', "&quot;")
                    text = '<span style="font-size:{}px">{}</span>'.format(safe, text)
            elif kind in ("fn", "fontName", "font-family"):
                if val:
                    safe = str(val).replace('"', "&quot;")
                    text = '<span style="font-family:{}">{}</span>'.format(safe, text)
            # 其他属性类型（如对齐、链接等）已在对应 convert_*_func 层级处理
        return text

    def convert_text_func(self, content) -> str:
        """正常文本、粗体、斜体、删除线、链接
        兼容段落缩进：如果 JSON content["4"] 带有段落缩进属性，
        在最终文本前加对应等级的 tab 前缀，避免有道笔记大纲式缩进被丢失。"""
        all_text = ""
        one_five_contents = content.get("5")
        if one_five_contents:
            for one_five_content in one_five_contents:
                # 包含 6 和 7
                two_five_contents = one_five_content.get("5")
                # 文本类型
                text_type = one_five_content.get("6")
                # 文本和属性
                seven_contents = one_five_content.get("7")

                text = ""
                # 有内嵌嵌套 five 时，先递归取出嵌套内容
                if two_five_contents and text_type == "li":
                    # 链接类型
                    source_text = self._get_common_text(one_five_content)
                    four_contents = one_five_content.get("4")
                    if four_contents and four_contents.get("hf"):
                        hf = four_contents.get("hf")
                        text = "[{}]({})".format(source_text, hf)
                    else:
                        text = source_text
                elif two_five_contents:
                    # 修复：同时有 seven_contents 和 two_five_contents 时，不再置空，
                    # 而是先取嵌套内容，再叠加本层 seven 文本
                    text = self._get_common_text(one_five_content)
                    if seven_contents:
                        for seven_content in seven_contents:
                            raw = seven_content.get("8") or ""
                            text_attrs = seven_content.get("9")
                            if raw and text_attrs:
                                raw = self._convert_text_attribute(raw, text_attrs)
                            text += raw
                elif seven_contents:
                    # 仅有 seven_contents：原样处理每个文本片段
                    for seven_content in seven_contents:
                        raw = seven_content.get("8") or ""
                        text_attrs = seven_content.get("9")
                        if raw and text_attrs:
                            raw = self._convert_text_attribute(raw, text_attrs)
                        text += raw
                if text:
                    all_text += text
        # 读取段落缩进（优先整段 content["4"]；每个 one_five_content["4"] 中也可能有，
        # 这里用最大的那个来做整体缩进，不会遗漏）
        level = 0
        top_props = content.get("4") if isinstance(content, dict) else None
        if top_props:
            level = max(level, _extract_indent_level(top_props))
        if one_five_contents:
            for fc in one_five_contents:
                p = fc.get("4") if isinstance(fc, dict) else None
                if p:
                    level = max(level, _extract_indent_level(p))
        if level and all_text:
            all_text = _apply_indent_prefix(all_text, level)
        return all_text

    def convert_h_func(self, content) -> str:
        """标题"""
        type_name = content.get("4").get("l")
        text = self._get_common_text(content=content)
        if text and type_name:
            level_str = type_name.replace("h", "")
            level = int(level_str)
            text = " ".join(["#" * int(level), text])
        return text

    def convert_im_func(self, content):
        """图片"""
        image_url = content["4"]["u"]
        return "![]({image_url})".format(image_url=image_url)

    def convert_a_func(self, content):
        """附件"""
        fn = content["4"]["fn"]
        fl = content["4"]["re"]
        return "[{text}]({resource_url})".format(text=fn, resource_url=fl)

    def convert_cd_func(self, content):
        """代码块"""
        language = content.get("4").get("la")
        codes: list = content.get("5")
        code_block = ""
        for code in codes:
            text = self._get_common_text(code)
            code_block += text + "\n"

        return "```{language}\r\n{code_block}```".format(
            language=language, code_block=code_block
        )

    def convert_la_func(self, content):
        """高亮块"""
        lines: list = content.get("5")
        highlight_block = ""
        for line in lines:
            text = self._get_common_text(line)
            highlight_block += text + "\n"

        return "```\r\n{highlight_block}```".format(highlight_block=highlight_block)

    def convert_q_func(self, content):
        """引用"""
        q_text_list = content["5"]
        text = ""
        for q_text_dict in q_text_list:
            q_text = self._get_common_text(q_text_dict)
            # 去除第一行的换行
            q_text = q_text.replace("\n", "")
            text += "> {q_text}\n".format(q_text=q_text)
        return text

    def convert_l_func(self, content):
        """有序/无序列表：按 JSON `content["4"]["ll"]`（list-level）写层级。
        用 HTML div padding-left 容器实现视觉缩进，保留原笔记的列表符号。"""
        text = self._get_common_text(content=content)
        props = content.get("4") if isinstance(content, dict) else {}
        level_raw = props.get("ll") if isinstance(props, dict) else None
        try:
            level = int(level_raw) if level_raw not in (None, "", 0, "0") else 0
        except (TypeError, ValueError):
            level = 0
        level = max(0, min(8, level))
        # 保留原笔记的列表符号（默认无序列表用 "- "）
        if level:
            line = "- " + text
        else:
            line = text
        if level:
            body = line.rstrip("\r\n")
            tail = line[len(body):]
            safe_px = str(level * 24)
            body = '<div style="padding-left:{}px">{}</div>'.format(safe_px, body)
            return body + tail
        return text

    def convert_t_func(self, content):
        """
        表格转换
        """
        nl = "\r\n"  # 考虑 Windows 系统，换行符设为 \r\n
        tr_list = content["5"]
        table_lines = ""

        for index, tc in enumerate(tr_list):
            table_content_list = tc["5"]
            table_content_len = len(table_content_list)
            if index == 1:
                table_line = "| -- " * table_content_len + "|\n| "
            else:
                table_line = "| "
            for table_content in table_content_list:
                table_text_list = table_content.get("5")[0].get("5")[0].get("7")
                if table_text_list:
                    table_text = table_text_list[0]["8"]
                else:
                    table_text = " "
                table_line = table_line + table_text + " | "
            table_lines = table_lines + table_line + f"{nl}"
        return table_lines

    # ----------------------------------------------------------------------
    # 白板/思维导图/内嵌流程图 扩展类型
    # ----------------------------------------------------------------------
    # 设计说明：
    #   1. 有道云笔记 .note 的 JSON 私有格式中，"6" 字段标记节点类型；
    #      excalidraw / drawio-ynote / mindmap 三个值表示白板或思维导图节点。
    #   2. 现有 _covert_json_to_markdown_content 已按 "6" 动态查找 convert_<type>_func，
    #      若不存在则回退到 convert_text_func（导致白板信息丢失）。
    #   3. 下面 3 个新方法仅为占位输出 + 资源 URL，风格与 convert_a_func 一致：
    #         - 不调网络（与现有 convert_*_func 风格一致，避免引入 session 依赖）
    #         - 失败兜底返回空字符串，不抛异常
    #         - 不修改 _covert_json_to_markdown_content 的分发逻辑
    #   4. 独立 .drawio 文件的导出仍由 pull.py 的 FileType.DRAWIO 分支处理，
    #      与本扩展完全独立，互不影响。
    # ----------------------------------------------------------------------

    # 资源类型 → 友好中文名
    _WHITEBOARD_TYPE_LABEL = {
        "excalidraw": "手绘白板",
        "drawio-ynote": "drawio 流程图",
        "mindmap": "思维导图",
    }

    # type ("6" 字段值) → 方法名 的映射表
    # 解决 "6" 字段值含连字符（如 "drawio-ynote"）无法做 Python 标识符的问题；
    # 不在此映射表中的 type，仍走原 dispatch 的 "convert_<type>_func" 字符串拼接，
    # 所以对既有 text/h/im/a/cd/la/q/l/t 等完全无影响。
    _WHITEBOARD_TYPE_FUNC_NAME = {
        "drawio-ynote": "convert_drawio_ynote_func",
    }

    @classmethod
    def _resolve_convert_func_name(cls, type_name):
        """
        根据 JSON "6" 字段值解析对应的 convert_*_func 方法名字符串。
        - 若 type_name 在 _WHITEBOARD_TYPE_FUNC_NAME 中（如 drawio-ynote），返回映射的方法名
        - 否则返回默认的 convert_<type_name>_func（与既有 dispatch 行为完全一致）
        返回的是字符串，由调用方 getattr 使用。
        """
        if not type_name:
            return ""
        if type_name in cls._WHITEBOARD_TYPE_FUNC_NAME:
            return cls._WHITEBOARD_TYPE_FUNC_NAME[type_name]
        return "convert_" + type_name + "_func"

    def _safe_attr(self, content, key, default=""):
        """统一从 content 中安全取字段，避免 KeyError"""
        try:
            v = content.get(key, default)
            if v is None:
                return default
            return v
        except Exception:
            return default

    def _lookup_whiteboard_path(self, node_id):
        """
        从 self._whiteboard_map 中按节点 id 查找本地 SVG 相对路径。
        找不到返回空串（让调用方走占位 markdown 引用块兜底）。
        """
        if not node_id:
            return ""
        m = getattr(self, "_whiteboard_map", None)
        if not m:
            return ""
        try:
            return m.get(node_id, "") or ""
        except Exception:
            return ""

    def convert_excalidraw_func(self, content) -> str:
        """
        excalidraw 手绘白板：
        - 若 self._whiteboard_map[node_id] 存在本地 SVG 路径，输出可缩放 <img>
          标签（浏览器/查看器天然支持滚轮/触摸手势缩放）
        - 否则回退到占位 markdown 引用块（含可点击 URL + 节点元信息）
        字段约定：
            content["3"]            节点 id
            content["4"].get("u")   资源 URL
        """
        try:
            node_id = self._safe_attr(content, "3")
            attr = content.get("4", {}) or {}
            resource_url = attr.get("u", "") if isinstance(attr, dict) else ""
            label = self._WHITEBOARD_TYPE_LABEL.get("excalidraw", "excalidraw")
            nl = "\r\n"
            # 优先输出内嵌 SVG（可缩放图片）
            local_path = self._lookup_whiteboard_path(node_id)
            if local_path:
                alt = label + " excalidraw " + node_id
                return _md_image(alt, local_path) + nl
            if not resource_url:
                return "> [" + label + " excalidraw] 节点 " + node_id + nl
            return (
                "> [" + label + " excalidraw](" + resource_url + ")" + nl +
                "> - 节点 id：`" + node_id + "`" + nl +
                "> - 类型：excalidraw 手绘白板" + nl +
                "> - 资源 URL：[点击在有道云笔记中打开](" + resource_url + ")" + nl
            )
        except Exception:
            return ""

    def convert_drawio_ynote_func(self, content) -> str:
        """
        drawio-ynote 流程图：
        - 若 self._whiteboard_map[node_id] 存在本地 SVG 路径，输出 <img>
        - 否则回退到占位 markdown 引用块
        字段约定：
            content["3"]            节点 id
            content["4"].get("u")   资源 URL
        备注：drawio-ynote 资源 content 字段是 data:image/svg+xml;base64,...
              可直接解码出可缩放 SVG。
        """
        try:
            node_id = self._safe_attr(content, "3")
            attr = content.get("4", {}) or {}
            resource_url = attr.get("u", "") if isinstance(attr, dict) else ""
            label = self._WHITEBOARD_TYPE_LABEL.get("drawio-ynote", "drawio 流程图")
            nl = "\r\n"
            local_path = self._lookup_whiteboard_path(node_id)
            if local_path:
                alt = label + " drawio-ynote " + node_id
                return _md_image(alt, local_path) + nl
            if not resource_url:
                return "> [" + label + " drawio-ynote] 节点 " + node_id + nl
            return (
                "> [" + label + " drawio-ynote](" + resource_url + ")" + nl +
                "> - 节点 id：`" + node_id + "`" + nl +
                "> - 类型：drawio 流程图（与独立 .drawio 同样可在 drawio 桌面版打开）" + nl +
                "> - 资源 URL：[点击在有道云笔记中打开](" + resource_url + ")" + nl
            )
        except Exception:
            return ""

    def convert_mindmap_func(self, content) -> str:
        """
        mindmap 思维导图：
        - 若 self._whiteboard_map[node_id] 存在本地 SVG 路径，输出 <img>
        - 否则回退到占位 markdown 引用块
        字段约定：
            content["3"]            节点 id
            content["4"].get("u")   资源 URL
        备注：mindmap 真实结构未确认，目前仅占位；后续若有真实样本可补充解析。
        """
        try:
            node_id = self._safe_attr(content, "3")
            attr = content.get("4", {}) or {}
            resource_url = attr.get("u", "") if isinstance(attr, dict) else ""
            label = self._WHITEBOARD_TYPE_LABEL.get("mindmap", "思维导图")
            nl = "\r\n"
            local_path = self._lookup_whiteboard_path(node_id)
            if local_path:
                alt = label + " mindmap " + node_id
                return _md_image(alt, local_path) + nl
            if not resource_url:
                return "> [" + label + " mindmap] 节点 " + node_id + nl
            return (
                "> [" + label + " mindmap](" + resource_url + ")" + nl +
                "> - 节点 id：`" + node_id + "`" + nl +
                "> - 类型：mindmap 思维导图（具体解析待真实样本验证）" + nl +
                "> - 资源 URL：[点击在有道云笔记中打开](" + resource_url + ")" + nl
            )
        except Exception:
            return ""


class YoudaoNoteConvert(object):
    """
    有道云笔记 note 内容转换为 markdown 内容
    """

    @staticmethod
    def covert_html_to_markdown(file_path):
        """
        转换 HTML 为 MarkDown
        :param file_path:
        :return:
        """
        with open(file_path, "rb") as f:
            content_str = f.read().decode("utf-8")
        from markdownify import markdownify as md

        # 严格保留模式：在 markdownify 之前，把 p/div/section 等节点的 CSS
        # padding-left/margin-left/text-indent 换算为行首 tab，避免 markdownify 吞缩进。
        # 同时在每个 <li> 上挂 data-md-indent，Phase 3 在 markdownify 之后把对应列表行补 tab。
        if PRESERVE_NATIVE_INDENT:
            content_str = _restore_html_css_indent(content_str)

        # 如果换行符丢失，使用 md(content_str.replace('<br>', '<br><br>').replace('</div>', '</div><br><br>')).rstrip()
        new_content = md(content_str)

        # Phase 3：按 <li> 进入顺序给 markdownify 输出的列表行补匹配数量的 tab
        if PRESERVE_NATIVE_INDENT:
            new_content = _apply_md_indent_markers(new_content)

        base = os.path.splitext(file_path)[0]
        new_file_path = "".join([base, MARKDOWN_SUFFIX])
        # 如果目标文件已存在，先删除（避免 Windows 下 os.rename 报错导致 .note 残留）
        if os.path.exists(new_file_path):
            os.remove(new_file_path)
        os.rename(file_path, new_file_path)
        with open(new_file_path, "wb") as f:
            f.write(new_content.encode())
        # 后处理：外置图片 → 软换行合并 → 裸文样式修复 → 双通道(Preserve native / flat→启发式回退)
        # 三入口均已在本函数内在段落/列表处写入有道原生缩进tab或Markdown列表前缀 → native_indent_written=True
        _apply_postprocess_chain(new_file_path, native_indent_written=True)

    @staticmethod
    def _covert_xml_to_markdown_content(file_path):
        # 使用 xml.etree.ElementTree 将 xml 文件转换为对象
        element_tree = ET.parse(file_path)
        note_element = element_tree.getroot()  # note Element

        # list_item 的 id 与 type 的对应
        list_item = {}
        for child in note_element[0]:
            if "list" in child.tag:
                list_item[child.attrib["id"]] = child.attrib["type"]

        body_element = note_element[1]  # Element
        new_content_list = []
        for element in list(body_element):
            # 使用 get_styled_text：提取 <text> 并在其上应用 <inline-styles> 的富文本样式
            text = XmlElementConvert.get_styled_text(element)
            name = element.tag.replace("{http://note.youdao.com}", "").replace("-", "_")
            convert_func = getattr(
                XmlElementConvert, "convert_{}_func".format(name), None
            )
            # 如果没有转换，只保留文字（已是富文本版本）
            if not convert_func:
                new_content_list.append(text)
                continue
            line_content = convert_func(text=text, element=element, list_item=list_item)
            new_content_list.append(line_content)
        return f"\r\n\r\n".join(new_content_list)  # 换行 1 行

    @staticmethod
    def covert_xml_to_markdown(file_path) -> bool:
        """
        转换 XML 为 MarkDown
        :param file_path:
        :return:
        """
        base = os.path.splitext(file_path)[0]
        new_file_path = "".join([base, MARKDOWN_SUFFIX])
        # 如果文件为空，结束
        if os.path.getsize(file_path) == 0:
            if os.path.exists(new_file_path):
                os.remove(new_file_path)
            os.rename(file_path, new_file_path)
            return False

        new_content = YoudaoNoteConvert._covert_xml_to_markdown_content(file_path)
        # 如果目标文件已存在，先删除（避免 Windows 下 os.rename 报错导致 .note 残留）
        if os.path.exists(new_file_path):
            os.remove(new_file_path)
        os.rename(file_path, new_file_path)
        with open(new_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
        # 后处理：外置图片 → 软换行合并 → 裸文样式修复 → 双通道(Preserve native / flat→启发式回退)
        # 三入口均已在本函数内在段落/列表处写入有道原生缩进tab或Markdown列表前缀 → native_indent_written=True
        _apply_postprocess_chain(new_file_path, native_indent_written=True)
        return True

    @staticmethod
    def _covert_json_to_markdown_content(file_path, whiteboard_map=None):
        new_content_list = []
        # 加载 json 文件
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                json_data = json.load(f)
            except Exception as e:
                logging.error(e)
                json_data = {}

        json_contents = json_data["5"]  # 3 代表 id，4 代表信息，5 代表内容，6 代表类型
        # 单次实例化，把 whiteboard_map 注入实例属性（兼容老调用方不传）
        json_converter = JsonConvert()
        if whiteboard_map and isinstance(whiteboard_map, dict):
            json_converter._whiteboard_map = whiteboard_map
        for content in json_contents:
            type = content.get("6")
            # 根据类型处理，无类型的为普通文本
            if type:
                # 通过 _resolve_convert_func_name 解析方法名，兼容 "drawio-ynote"
                # 这类含连字符的 type；其它 type 与原行为完全一致（默认拼接 convert_<type>_func）
                func_name = JsonConvert._resolve_convert_func_name(type)
                convert_func = getattr(json_converter, func_name, None) if func_name else None
                # 如果类型没有对应转换函数，只保留文字
                if not convert_func:
                    line_content = json_converter.convert_text_func(content)
                else:
                    line_content = convert_func(content)
            else:
                line_content = json_converter.convert_text_func(content)

            # 判断是否有内容
            if line_content:
                new_content_list.append(line_content)
        return f"\r\n\r\n".join(new_content_list)  # 换行 1 行

    @staticmethod
    def covert_json_to_markdown(file_path, whiteboard_map=None) -> str:
        """
        转换 Json 为 MarkDown

        :param file_path: .note 私有 JSON 路径
        :param whiteboard_map: 预渲染的白板本地路径映射 {node_id: "images/<id>.svg"}；
                               传入后，excalidraw/drawio-ynote/mindmap 节点会输出
                               <img src="images/<id>.svg" /> 内嵌引用，SVG 天然支持
                               滚轮/触摸手势缩放；不传则走原占位 markdown 引用块。
        :return: 生成的 .md 绝对路径
        """
        base = os.path.splitext(file_path)[0]
        new_file_path = "".join([base, MARKDOWN_SUFFIX])
        # 如果文件为空，结束
        if os.path.getsize(file_path) == 0:
            if os.path.exists(new_file_path):
                os.remove(new_file_path)
            os.rename(file_path, new_file_path)
            return False
        new_content = YoudaoNoteConvert._covert_json_to_markdown_content(
            file_path, whiteboard_map=whiteboard_map
        )
        with open(new_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
        # 删除旧文件
        if os.path.exists(file_path):
            os.remove(file_path)
        # 后处理：外置图片 → 软换行合并 → 裸文样式修复 → 双通道(Preserve native / flat→启发式回退)
        # 三入口均已在本函数内在段落/列表处写入有道原生缩进tab或Markdown列表前缀 → native_indent_written=True
        _apply_postprocess_chain(new_file_path, native_indent_written=True)
        return new_file_path
