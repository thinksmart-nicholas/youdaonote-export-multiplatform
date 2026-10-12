# -*- coding: utf-8 -*-
"""有道 XML 格式 note 的转换规则。"""

import json

from .indent import _extract_indent_level  # noqa: F401
from .style import flatten_nested_spans, merge_style_strings, normalize_color_value  # noqa: F401


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


    # ---------- 样式工具委托（实现已在 style.py）----------
    # JsonConvert / get_styled_text 均通过类名访问这三个方法，
    # 保留类方法签名可让调用方无需改动。
    @staticmethod
    def _merge_style_strings(styles):
        return merge_style_strings(styles)

    @classmethod
    def _flatten_nested_spans(cls, text):
        return flatten_nested_spans(text)

    @staticmethod
    def _normalize_color_value(value):
        return normalize_color_value(value)

