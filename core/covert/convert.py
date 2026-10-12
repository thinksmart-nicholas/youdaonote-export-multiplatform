# -*- coding: utf-8 -*-
"""转换入口：HTML / XML / JSON 三种 .note -> Markdown。"""

import json
import logging
import os
import xml.etree.ElementTree as ET

from .indent import _apply_md_indent_markers, _restore_html_css_indent  # noqa: F401
from .json_convert import JsonConvert  # noqa: F401
from .postprocess import _apply_postprocess_chain  # noqa: F401
from .util import MARKDOWN_SUFFIX, PRESERVE_NATIVE_INDENT  # noqa: F401
from .xml_convert import XmlElementConvert  # noqa: F401



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
