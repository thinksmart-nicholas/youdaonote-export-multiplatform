# -*- coding: utf-8 -*-
"""有道 JSON 格式 note 的转换规则。"""

from typing import Tuple

from .indent import _apply_indent_prefix, _extract_indent_level  # noqa: F401
from .util import _md_image  # noqa: F401
from .xml_convert import XmlElementConvert  # noqa: F401



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
