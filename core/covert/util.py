# -*- coding: utf-8 -*-
"""公共常量与小工具：导出开关、Markdown 图片语法、base64 阈值、缩进键名等。"""

import re

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
