import json
import logging
import os
import xml.etree.ElementTree as ET
from typing import Tuple

MARKDOWN_SUFFIX = ".md"


class XmlElementConvert(object):
    """
    XML Element 转换规则
    """

    @staticmethod
    def convert_para_func(**kwargs):
        """正常文本（粗体、斜体、删除线、链接）"""
        return kwargs.get("text")

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
        """列表"""
        list_id = kwargs.get("element").attrib["list-id"]
        is_ordered = kwargs.get("list_item").get(list_id)
        text = kwargs.get("text")
        if is_ordered == "unordered":
            return "- {text}".format(text=text)
        elif is_ordered == "ordered":
            return "1. {text}".format(text=text)

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
           替代原先只取纯文本的 get_text_by_key。"""
        text = XmlElementConvert.get_text_by_key(list(element))
        return XmlElementConvert._apply_xml_inline_styles(text, element)


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
        return all_text

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
        """正常文本、粗体、斜体、删除线、链接"""
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
        """有序列表和无序列表，有序列表转成无序列表"""
        text = self._get_common_text(content=content)
        is_ordered = content.get("4").get("lt")
        if is_ordered == "unordered":
            level = content.get("4").get("ll")
            return "\t" * (level - 1) + "- {text}".format(text=text)
        elif is_ordered == "ordered":
            # 有序列表都设置为 1，有些 MD 编辑自动转为有序列表
            return "1. {text}".format(text=text)

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

        # 如果换行符丢失，使用 md(content_str.replace('<br>', '<br><br>').replace('</div>', '</div><br><br>')).rstrip()
        new_content = md(content_str)
        base = os.path.splitext(file_path)[0]
        new_file_path = "".join([base, MARKDOWN_SUFFIX])
        # 如果目标文件已存在，先删除（避免 Windows 下 os.rename 报错导致 .note 残留）
        if os.path.exists(new_file_path):
            os.remove(new_file_path)
        os.rename(file_path, new_file_path)
        with open(new_file_path, "wb") as f:
            f.write(new_content.encode())

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
        return True

    @staticmethod
    def _covert_json_to_markdown_content(file_path):
        new_content_list = []
        # 加载 json 文件
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                json_data = json.load(f)
            except Exception as e:
                logging.error(e)
                json_data = {}

        json_contents = json_data["5"]  # 3 代表 id，4 代表信息，5 代表内容，6 代表类型
        for content in json_contents:
            type = content.get("6")
            # 根据类型处理，无类型的为普通文本
            if type:
                convert_func = getattr(
                    JsonConvert(), "convert_{}_func".format(type), None
                )
                # 如果类型没有对应转换函数，只保留文字
                if not convert_func:
                    line_content = JsonConvert().convert_text_func(content)
                else:
                    line_content = convert_func(content)
            else:
                line_content = JsonConvert().convert_text_func(content)

            # 判断是否有内容
            if line_content:
                new_content_list.append(line_content)
        return f"\r\n\r\n".join(new_content_list)  # 换行 1 行

    @staticmethod
    def covert_json_to_markdown(file_path) -> str:
        """
        转换 Json 为 MarkDown
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
        new_content = YoudaoNoteConvert._covert_json_to_markdown_content(file_path)
        with open(new_file_path, "wb") as f:
            f.write(new_content.encode("utf-8"))
        # 删除旧文件
        if os.path.exists(file_path):
            os.remove(file_path)
        return new_file_path
