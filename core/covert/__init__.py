# -*- coding: utf-8 -*-
"""有道云笔记 .note -> Markdown 的转换包。

原先全部实现集中在单个 covert.py（2900+ 行），现按职责拆分为：

    util.py         公共常量与小工具
    style.py        共享样式工具（style 合并 / span 扁平化 / 颜色规范化）
    indent.py       缩进体系（原生缩进字段还原 + 历史 md 启发式层级恢复）
    postprocess.py  Markdown 生成后的统一后处理链
    xml_convert.py  XML 格式 note 的转换规则
    json_convert.py JSON 格式 note 的转换规则
    convert.py      三个对外转换入口

本模块是**稳定门面**：外部只需 `from core.covert import YoudaoNoteConvert`，
请勿依赖 `from core.covert.xxx import` 的内部结构（后续可能继续调整内部模块的划分）。
"""

from .convert import YoudaoNoteConvert
from .json_convert import JsonConvert
from .util import MARKDOWN_SUFFIX, PRESERVE_NATIVE_INDENT
from .xml_convert import XmlElementConvert

__all__ = [
    "YoudaoNoteConvert",
    "XmlElementConvert",
    "JsonConvert",
    "PRESERVE_NATIVE_INDENT",
    "MARKDOWN_SUFFIX",
]
