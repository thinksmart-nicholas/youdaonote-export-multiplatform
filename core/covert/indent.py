# -*- coding: utf-8 -*-
"""缩进体系：有道原生缩进字段的读取与还原，以及历史 md 的启发式层级恢复。"""

import logging
import os
import re

from .util import PRESERVE_NATIVE_INDENT, _HTML_INDENT_PX_PER_LEVEL, _INDENT_LEVEL_KEYS  # noqa: F401



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


# 有序列表条目：1-3 位数字 + 点/顿号 + 非数字（排除小数 "1.5"），点后可带空格
_RE_NUM_ITEM = re.compile(r'^\d{1,3}[\.．、](?!\d)\s*')


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
