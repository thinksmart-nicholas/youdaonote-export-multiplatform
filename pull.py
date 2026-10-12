#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import logging
import os
import platform
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from enum import Enum
from typing import Tuple

import requests

try:
    # 非 Windows 平台（Linux / macOS）没有 win32_setctime，不能无条件导入，
    # 否则在这些平台上 import pull 就会直接失败
    from win32_setctime import setctime
except Exception:
    setctime = None

from core import log
from core.api import YoudaoNoteApi
from core.common import get_script_directory, resolve_data_file
from core.covert import YoudaoNoteConvert
from core.image import ImagePull
from core.whiteboard_render import process_and_save as _whiteboard_process_and_save

__author__ = "Depp Wang (deppwxq@gmail.com)"
__github__ = "https//github.com/DeppWang/youdaonote-pull"

MARKDOWN_SUFFIX = ".md"

# config.json 必需项：缺失即报错
REQUIRED_CONFIG_KEYS = [
    "local_dir",
    "ydnote_dir",
    "smms_secret_token",
    "is_relative_path",
]
# config.json 可选项：缺失时用默认值补齐
OPTIONAL_CONFIG_KEYS = {
    "is_base64_image": False,
}


class FileType(Enum):
    OTHER = 0
    MARKDOWN = 1
    XML = 2
    JSON = 3
    DRAWIO = 4  # drawio 桌面版/有道白板导出的 .drawio（SVG+mxfile）


class FileActionEnum(Enum):
    CONTINUE = "跳过"
    ADD = "新增"
    UPDATE = "更新"


class YoudaoNotePull(object):
    """
    有道云笔记 Pull 封装
    """

    def __init__(self):
        self.root_local_dir = None  # 本地文件根目录
        self.youdaonote_api = None
        self.smms_secret_token = None
        self.is_relative_path = None  # 是否使用相对路径
        self.is_base64_image = None  # 是否将图片内嵌为 base64

    def _covert_config(self, config_path=None) -> Tuple[dict, str]:
        """
        转换配置文件为 dict
        :param config_path: config 文件路径
        :return: (config_dict, error_msg)
        """

        config_path = (
            config_path
            if config_path
            else resolve_data_file("config.json")
        )
        with open(config_path, "rb") as f:
            config_str = f.read().decode("utf-8")

        try:
            config_dict = json.loads(config_str)
        except Exception:
            return (
                {},
                "请检查「config.json」格式是否为 utf-8 格式的 json！建议使用 Sublime 编辑「config.json」",
            )

        # 必需项缺失才报错；可选项补齐默认值，避免新增配置就让老配置失效
        missing_keys = [key for key in REQUIRED_CONFIG_KEYS if key not in config_dict]
        if missing_keys:
            return (
                {},
                "「config.json」缺少必需的配置项：{}，请参考「config.example.json」补齐".format(
                    "、".join(missing_keys)
                ),
            )
        for key, default_value in OPTIONAL_CONFIG_KEYS.items():
            config_dict.setdefault(key, default_value)
        return config_dict, ""

    def _check_local_dir(self, local_dir, test_default_dir=None) -> Tuple[str, str]:
        """
        检查本地文件夹
        :param local_dir: 本地文件夹名（绝对路径）
        :return: local_dir, error_msg
        """
        # 如果没有指定本地文件夹，当前目录新增 youdaonote 目录
        if not local_dir:
            add_dir = test_default_dir if test_default_dir else "youdaonote"
            # 兼容 Windows 系统，将路径分隔符（\\）替换为 /
            local_dir = os.path.join(get_script_directory(), add_dir).replace("\\", "/")

        # 如果指定的本地文件夹不存在，创建文件夹
        if not os.path.exists(local_dir):
            try:
                os.mkdir(local_dir)
            except:
                return "", "请检查「{}」上层文件夹是否存在，并使用绝对路径！".format(local_dir)
        return local_dir, ""

    @staticmethod
    def _split_ydnote_dir(ydnote_dir):
        """
        把 ydnote_dir 拆成逐级目录名。

        - 同时接受 "/" 与 "\\" 作为分隔符（Windows 用户习惯写 "\\"）
        - 忽略首尾分隔符、连续重复分隔符，以及每段两侧的空白
        - 空值或纯分隔符返回 []，表示根目录

        :param ydnote_dir: config.json 中 ydnote_dir 的原始值
        :return: 逐级目录名列表
        """
        if not ydnote_dir or not isinstance(ydnote_dir, str):
            return []
        text = ydnote_dir.replace("\\", "/").strip()
        return [seg.strip() for seg in text.split("/") if seg.strip()]

    def _get_ydnote_dir_id(self, ydnote_dir) -> Tuple[str, str]:
        """
        获取指定有道云笔记目录 ID

        :param ydnote_dir: 指定有道云笔记目录。支持多级路径，用 "/" 或 "\\" 分隔，
                           如 "工作/2026/项目A"；不含分隔符时按顶层目录名精确匹配
                           （与旧行为一致）。留空表示根目录，导出所有笔记。
        :return: dir_id, error_msg
        """
        root_dir_info = self.youdaonote_api.get_root_dir_info_id()
        root_dir_id = root_dir_info["fileEntry"]["id"]

        segments = self._split_ydnote_dir(ydnote_dir)

        # 如果不指定文件夹，取根目录 ID
        if not segments:
            return root_dir_id, ""

        dir_id = root_dir_id
        for depth, name in enumerate(segments, start=1):
            dir_info = self.youdaonote_api.get_dir_info_by_id(dir_id)
            next_id = ""
            for entry in dir_info.get("entries") or []:
                file_entry = entry.get("fileEntry") or {}
                if file_entry.get("name") != name:
                    continue
                # 中间层级必须真的是文件夹，否则无法继续向下一层找
                if depth < len(segments) and not file_entry.get("dir"):
                    return "", "有道云笔记路径「{}」中的「{}」不是文件夹，无法继续查找下一层".format(
                        "/".join(segments), name
                    )
                next_id = file_entry.get("id") or ""
                break

            if not next_id:
                # 单级时沿用旧的错误文案，避免影响既有调用方与用例
                if len(segments) == 1:
                    return "", "有道云笔记指定顶层目录不存在"
                return "", "有道云笔记指定目录不存在：{}（第 {} 层「{}」未找到）".format(
                    "/".join(segments), depth, name
                )
            dir_id = next_id

        return dir_id, ""

    def get_ydnote_dir_id(self) -> Tuple[str, str]:
        """
        获取有道云笔记根目录或指定目录 ID
        :return:
        """
        config_dict, error_msg = self._covert_config()
        if error_msg:
            return "", error_msg
        local_dir, error_msg = self._check_local_dir(local_dir=config_dict["local_dir"])
        if error_msg:
            return "", error_msg
        self.root_local_dir = local_dir
        self.youdaonote_api = YoudaoNoteApi()
        error_msg = self.youdaonote_api.login_by_cookies()
        logging.info("本次使用 Cookies 登录")
        if error_msg:
            return "", error_msg
        self.smms_secret_token = config_dict["smms_secret_token"]
        self.is_relative_path = config_dict["is_relative_path"]
        self.is_base64_image = config_dict["is_base64_image"]
        return self._get_ydnote_dir_id(ydnote_dir=config_dict["ydnote_dir"])

    def _judge_type(self, file_id, youdao_file_suffix) -> Enum:
        """
        判断笔记类型
        :param file_id:
        :param youdao_file_suffix:
        :return:
        """
        file_type = FileType.OTHER
        # 1、如果文件是 .md 类型
        if youdao_file_suffix == MARKDOWN_SUFFIX:
            file_type = FileType.MARKDOWN
            return file_type
        elif youdao_file_suffix == ".drawio":
            # 独立 drawio 文件：drawio 桌面版 / 有道白板导出的 .drawio
            # 内容通常是 SVG 内嵌 base64 mxfile XML；标记为 DRAWIO，由 _pull_file 独立处理
            # 不影响 XML/JSON/HTML/Markdown 任何转换路径
            return FileType.DRAWIO
        elif (
            youdao_file_suffix == ".note"
            or youdao_file_suffix == ".clip"
            or youdao_file_suffix == ""
        ):
            response = self.youdaonote_api.get_file_by_id(file_id)
            # 2、如果文件以 `<?xml` 开头
            if response.content[:5] == b"<?xml":
                file_type = FileType.XML
            # 3、如果文件以 `{` 开头
            elif response.content.startswith(b'{"'):
                file_type = FileType.JSON
        return file_type

    def _get_file_action(self, local_file_path, modify_time) -> Enum:
        """
        获取文件操作行为
        :param local_file_path:
        :param modify_time:
        :return: FileActionEnum
        """
        # 如果不存在，则下载
        if not os.path.exists(local_file_path):
            return FileActionEnum.ADD

        # 如果已经存在，判断是否需要更新
        # 如果有道云笔记文件更新时间小于本地文件时间，说明没有更新，则不下载，跳过
        if modify_time <= os.path.getmtime(local_file_path):
            logging.info("此文件「%s」不更新，跳过", local_file_path)
            return FileActionEnum.CONTINUE
        # 同一目录存在同名 md 和 note 文件时，后更新文件将覆盖另一个
        return FileActionEnum.UPDATE

    def _optimize_file_name(self, name) -> str:
        """
        优化文件名
        :param name:
        :return:
        """
        # 替换下划线
        regex_symbol = re.compile(r"[<]")  # 符号： <
        # 删除特殊字符
        del_regex_symbol = re.compile(r'[\\/":\|\*\?#>]')  # 符号：\ / " : | * ? # >
        # 首尾的空格
        name = name.replace("\n", "")
        # 去除换行符
        name = name.strip()
        # 替换一些特殊符号
        name = regex_symbol.sub("_", name)
        name = del_regex_symbol.sub("", name)
        return name

    def _restore_file_times(self, file_path, create_time, modify_time) -> None:
        """
        把有道云笔记的时间回填到本地文件。

        必须同时回填修改时间（mtime）：`_get_file_action` 依赖
        `modify_time <= os.path.getmtime()` 判断是否需要更新，只回填创建时间
        会让本地 mtime 停在本次写入时刻，导致笔记更新后仍被判为「跳过」。

        Windows 下 os.utime 无法设置创建时间（ctime），需要额外用
        win32_setctime 补齐，且必须放在 os.utime 之后，否则会被覆盖。

        :param file_path: 本地文件路径
        :param create_time: 有道云笔记创建时间（秒级时间戳）
        :param modify_time: 有道云笔记修改时间（秒级时间戳）
        """
        try:
            os.utime(file_path, (create_time, modify_time))
        except Exception as error:
            logging.info("回填「{}」修改时间失败：{}".format(file_path, format(error)))

        if platform.system() != "Windows" or setctime is None:
            return
        try:
            setctime(file_path, create_time)
        except Exception as error:
            logging.info("回填「{}」创建时间失败：{}".format(file_path, format(error)))

    def _add_or_update_file(
        self, file_id, file_name, local_dir, modify_time, create_time
    ):
        """
        新增或更新文件

        :param file_id:
        :param file_name:
        :param local_dir:
        :param modify_time:
        :param create_time:
        :return: (file_action, local_file_path, error_msg)
                 file_action 为 CONTINUE 表示已跳过未更新；
                 error_msg 非空表示该文件处理失败。
                 命令行入口不使用返回值，返回值供导出引擎统计进度。
        """
        file_name = self._optimize_file_name(file_name)
        youdao_file_suffix = os.path.splitext(file_name)[1]  # 笔记后缀
        original_file_path = os.path.join(local_dir, file_name).replace(
            "\\", "/"
        )  # 原后缀路径

        # 所有类型文件均下载，不做处理
        file_type = self._judge_type(file_id, youdao_file_suffix)

        # 「文档」类型本地文件均已 .md 结尾
        local_file_path = (
            os.path.join(
                local_dir, "".join([os.path.splitext(file_name)[0], MARKDOWN_SUFFIX])
            ).replace("\\", "/")
            if file_type != FileType.OTHER
            else original_file_path
        )

        # 如果有有道云笔记是「文档」类型，则提示类型
        tip = (
            "，云笔记原格式为 {}".format(file_type.name) if file_type != FileType.OTHER else ""
        )

        file_action = self._get_file_action(local_file_path, modify_time)
        if file_action == FileActionEnum.CONTINUE:
            return file_action, local_file_path, ""
        if file_action == FileActionEnum.UPDATE:
            # 考虑到使用 f.write() 直接覆盖原文件，在 Windows 下报错（WinError 183），先将其删除
            os.remove(local_file_path)
        try:
            self._pull_file(
                file_id,
                original_file_path,
                local_file_path,
                file_type,
                youdao_file_suffix,
            )
            # 上面 CONTINUE 已提前返回，此处只可能是 ADD / UPDATE
            logging.info("{}「{}」{}".format(file_action.value, local_file_path, tip))

            # 本地文件时间设置为有道云笔记的时间
            self._restore_file_times(local_file_path, create_time, modify_time)
            return file_action, local_file_path, ""

        except Exception as error:
            logging.info(
                "{}「{}」可能失败！请检查文件！错误提示：{}".format(
                    file_action.value, original_file_path, format(error)
                )
            )
            return file_action, local_file_path, format(error)

    def _gen_drawio_companion_md(self, file_path: str) -> None:
        """
        为独立 .drawio 文件生成 companion .md，描述文件位置/类型/可编辑工具。
        设计原则：
          1. 完全不读取、修改 .drawio 本体（保留 drawio 桌面版输出供再编辑）
          2. 不调用 YoudaoNoteConvert / ImagePull，不影响 XML/JSON/HTML 任何转换路径
          3. 输出 UTF-8 纯文本 md，包含文件名、相对路径、说明；失败时仅记日志、不抛异常
        """
        try:
            base = os.path.splitext(file_path)[0]
            md_path = base + MARKDOWN_SUFFIX
            # 路径相对化：相对于 md 所在目录，方便在 markdown 查看器中点击
            rel = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            # 探测内容形态：drawio 桌面版输出 = SVG+base64 mxfile
            head = ""
            try:
                with open(file_path, "rb") as f:
                    head = f.read(200).decode("utf-8", errors="replace")
            except Exception:
                head = ""
            is_svg_embed = head.startswith("data:image/svg+xml;base64,") or "<svg" in head[:100]
            kind = "drawio 桌面版 SVG（内嵌 mxfile XML）" if is_svg_embed else "drawio XML"
            body = (
                "# {name}\r\n\r\n"
                "- 原始文件：`{rel}`\r\n"
                "- 形态：{kind}\r\n"
                "- 大小：{size} 字节\r\n"
                "- 用 [drawio 桌面版](https://github.com/jgraph/drawio-desktop) 打开可继续编辑\r\n"
            ).format(
                name=os.path.basename(base),
                rel=rel,
                kind=kind,
                size=file_size,
            )
            with open(md_path, "wb") as f:
                f.write(body.encode("utf-8"))
            logging.info("已为 drawio 文件生成 companion md: {}".format(md_path))
        except Exception as err:
            logging.info("为 drawio 文件生成 companion md 失败: {}".format(repr(err)))

    # 白板节点 type 名（与 core.covert.JsonConvert._WHITEBOARD_TYPE_LABEL 保持一致）
    _WHITEBOARD_NODE_TYPES = ("excalidraw", "drawio-ynote", "mindmap")

    def _prefetch_whiteboards(self, file_path, images_dir):
        """
        扫描 .note 私有 JSON 中的白板/思维导图/内嵌流程图节点（excalidraw /
        drawio-ynote / mindmap），下载并渲染为本地 SVG，保存到 images_dir。

        返回 {node_id: "images/<id>.svg"} 映射，供 covert_json_to_markdown 用
        来生成 <img src="..." /> 内嵌引用（SVG 天然支持滚轮/触摸手势缩放）。

        设计原则：
          1. 完全不修改 .note 源文件（仅读）
          2. 任何节点下载/渲染失败都跳过该节点，不影响主流程
          3. 兼容老 JSON 中无 "6" 字段或非白板 type 的节点
        """
        result = {}
        if not file_path or not os.path.isfile(file_path):
            return result
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                try:
                    data = json.load(f)
                except Exception as e:
                    logging.info("预下载白板：JSON 解析失败（%s），跳过", e)
                    return result
        except Exception as e:
            logging.info("预下载白板：读取文件失败（%s），跳过", e)
            return result

        nodes = data.get("5") if isinstance(data, dict) else None
        if not isinstance(nodes, list):
            return result

        for node in nodes:
            if not isinstance(node, dict):
                continue
            node_type = node.get("6")
            if node_type not in self._WHITEBOARD_NODE_TYPES:
                continue
            node_id = node.get("3") or node.get("id")
            if not node_id:
                continue
            attr = node.get("4") or {}
            url = attr.get("u") if isinstance(attr, dict) else None
            if not url:
                continue
            try:
                rel_path = _whiteboard_process_and_save(
                    self.youdaonote_api,
                    node_type,
                    url,
                    images_dir,
                    str(node_id),
                )
            except Exception as e:
                logging.info("白板预下载失败 type=%s id=%s err=%s", node_type, node_id, e)
                rel_path = ""
            if rel_path:
                result[str(node_id)] = rel_path
                logging.info(
                    "白板预下载成功 type=%s id=%s -> %s", node_type, node_id, rel_path
                )
        return result

    def _pull_file(
        self, file_id, file_path, local_file_path, file_type, youdao_file_suffix
    ):
        """
        下载文件
        :param file_id:
        :param file_path:
        :param local_file_path: 本地
        :param file_type:
        :param youdao_file_suffix:
        :return:
        """
        # 1、所有的都先下载
        response = self.youdaonote_api.get_file_by_id(file_id)
        with open(file_path, "wb") as f:
            f.write(response.content)  # response.content 本身就是字节类型

        # 2、如果文件是 note 类型，将其转换为 MarkDown 类型
        if file_type == FileType.XML:
            try:
                YoudaoNoteConvert.covert_xml_to_markdown(file_path)
            except ET.ParseError:
                logging.info("此 note 笔记应该为 17 年以前新建，格式为 html，将转换为 Markdown ...")
                YoudaoNoteConvert.covert_html_to_markdown(file_path)
            except Exception as e:
                logging.info("note 笔记转换 MarkDown 失败，将跳过 {}".format(repr(e)))
        elif file_type == FileType.JSON:
            # 预下载白板资源（excalidraw/drawio-ynote/mindmap）→ images/<id>.svg，
            # 转换时由 convert_*_func 优先输出 <img src="images/<id>.svg" />，
            # SVG 天然支持滚轮/触摸手势缩放。预下载失败不影响 covert 主流程。
            images_dir = os.path.dirname(local_file_path)
            images_dir = os.path.join(images_dir, "images").replace("\\", "/")
            whiteboard_map = self._prefetch_whiteboards(file_path, images_dir)
            YoudaoNoteConvert.covert_json_to_markdown(file_path, whiteboard_map=whiteboard_map)
        elif file_type == FileType.DRAWIO:
            # 独立 .drawio 文件：不转换 .drawio 本体（保留原始文件供 drawio 工具再编辑），
            # 生成 companion .md 指向 .drawio 的相对路径，供 Markdown 查看器索引。
            # 完全不调用 YoudaoNoteConvert，不影响 XML/JSON/HTML 任何转换路径。
            self._gen_drawio_companion_md(file_path)

        # 3、迁移文本文件里面的有道云笔记图片（链接）
        # DRAWIO 类型不调用 ImagePull：.drawio 本身是 SVG/mxfile 二进制，无 note.youdao.com 图片链接
        if (file_type != FileType.OTHER and file_type != FileType.DRAWIO) or youdao_file_suffix == MARKDOWN_SUFFIX:
            imagePull = ImagePull(
                self.youdaonote_api,
                self.smms_secret_token,
                self.is_relative_path,
                self.is_base64_image,
                self.root_local_dir,
            )
            imagePull.migration_ydnote_url(local_file_path)


if __name__ == "__main__":
    # 局部导入：core.exporter 反过来会 import pull，放在模块顶层会形成循环导入
    from core.exporter import Exporter

    log.init_logging()

    start_time = int(time.time())

    try:
        youdaonote_pull = YoudaoNotePull()
        ydnote_dir_id, error_msg = youdaonote_pull.get_ydnote_dir_id()
        if error_msg:
            logging.info(error_msg)
            sys.exit(1)
        logging.info("正在 pull，请稍后 ...")

        # 与图形界面共用同一套导出引擎：配置解析、登录、目录定位沿用
        # YoudaoNotePull，遍历与导出交给 Exporter（支持选择性导出与进度回调）
        exporter = Exporter()
        exporter.api = youdaonote_pull.youdaonote_api
        exporter.pull = youdaonote_pull
        tree, error_msg = exporter.load_tree(ydnote_dir_id)
        if error_msg:
            logging.info(error_msg)
            sys.exit(1)
        report = exporter.export(tree, {ydnote_dir_id}, youdaonote_pull.root_local_dir)
        logging.info("导出结果：{}".format(report))
    except requests.exceptions.ProxyError:
        logging.info(
            "请检查网络代理设置；也有可能是调用有道云笔记接口次数达到限制，请等待一段时间后重新运行脚本，若一直失败，可删除「cookies.json」后重试"
        )
        traceback.print_exc()
        logging.info("已终止执行")
        sys.exit(1)
    except requests.exceptions.ConnectionError:
        logging.info("网络错误，请检查网络是否正常连接。若突然执行中断，可忽略此错误，重新运行脚本")
        traceback.print_exc()
        logging.info("已终止执行")
        sys.exit(1)
    # 链接错误等异常
    except Exception as err:
        logging.info("Cookies 可能已过期！其他错误：{}".format(format(err)))
        traceback.print_exc()
        logging.info("已终止执行")
        sys.exit(1)

    end_time = int(time.time())
    logging.info("运行完成！耗时 {} 秒".format(str(end_time - start_time)))
