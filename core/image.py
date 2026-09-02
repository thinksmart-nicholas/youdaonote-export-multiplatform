import base64
import logging
import os
import re
from typing import Tuple
from urllib import parse
from urllib.parse import urlparse

import requests

REGEX_IMAGE_URL = re.compile(r"!\[.*?\]\((.*?note\.youdao\.com.*?)\)")
REGEX_ATTACH = re.compile(r"\[(.*?)\]\(((http|https)://note\.youdao\.com.*?)\)")
REGEX_FILE_ABSOLUTE = re.compile(r"!\[.*?\]\((file:///.*?)\)")
# 有道云笔记的图片地址
IMAGES = "images"
# 有道云笔记的附件地址
ATTACH = "attachments"
# 图片文件名中的特殊字符（会导致 Markdown 链接截断）
REGEX_SPECIAL_CHARS = re.compile(r'[(){}\[\]$~@%`^]')

# base64 图片 MIME 类型映射
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


class ImagePull:
    def __init__(
        self,
        youdaonote_api,
        smms_secret_token: str,
        is_relative_path: bool,
        is_base64_image: bool = False,
        root_local_dir: str = None,
    ):
        self.youdaonote_api = youdaonote_api
        self.smms_secret_token = smms_secret_token
        self.is_relative_path = is_relative_path
        self.is_base64_image = is_base64_image
        self.root_local_dir = root_local_dir

    @classmethod
    def _url_encode(cls, file_path: str):
        """对一些特殊字符 url 编码
        :param file_path:
        """
        file_path = file_path.replace(" ", "%20")
        return file_path

    @classmethod
    def _sanitize_filename(cls, filename: str) -> str:
        """清理文件名中的特殊字符，防止 Markdown 链接截断
        将 (){}[]$~@%`^ 等字符替换为 _
        :param filename: 原始文件名
        :return: 清理后的文件名
        """
        return REGEX_SPECIAL_CHARS.sub("_", filename)

    def migration_ydnote_url(self, file_path):
        """
        迁移有道云笔记文件 URL
        - 图片：根据 is_base64_image 配置，下载为本地文件或内嵌为 base64
        - 附件：下载到本地 attachments 目录
        - 清理 file:/// 绝对路径引用
        :param file_path:
        :return:
        """

        # 文件内容为空，也下载到本地
        with open(file_path, "rb") as f:
            content = f.read().decode("utf-8")

        # 清理 file:/// 绝对路径引用（指向原作者本地文件，无法访问）
        file_absolute_refs = REGEX_FILE_ABSOLUTE.findall(content)
        if file_absolute_refs:
            logging.info("正在清理「{}」中的 file:/// 本地路径引用...".format(file_path))
            for ref in file_absolute_refs:
                content = content.replace(
                    "![]({})".format(ref), ""
                ).replace(
                    "![]({})\n".format(ref), ""
                )

        # 图片
        image_urls = REGEX_IMAGE_URL.findall(content)
        if len(image_urls) > 0:
            logging.info("正在转换有道云笔记「{}」中的有道云图片链接...".format(file_path))
        for image_url in image_urls:
            if self.is_base64_image:
                # base64 模式：下载图片到内存，编码为 data URI
                data_uri = self._download_image_as_base64(image_url)
                if data_uri:
                    content = content.replace(image_url, data_uri)
                    continue
                # base64 失败，回退到本地下载
                logging.info("base64 编码失败，回退到本地下载: {}".format(image_url))

            # 本地文件模式：下载图片到 images 目录
            try:
                image_path = self._get_new_image_path(file_path, image_url)
            except Exception as error:
                logging.info(
                    "下载图片「{}」可能失败！请检查图片！错误提示：{}".format(image_url, format(error))
                )
            if image_url == image_path:
                continue
            # 将绝对路径替换为相对路径
            if self.is_relative_path and not self.smms_secret_token:
                image_path = image_path[image_path.find(IMAGES):]

            image_path = self._url_encode(image_path)
            content = content.replace(image_url, image_path)

        # 附件
        attach_name_and_url_list = REGEX_ATTACH.findall(content)
        if len(attach_name_and_url_list) > 0:
            logging.info("正在转换有道云笔记「{}」中的有道云附件链接...".format(file_path))
        for attach_name_and_url in attach_name_and_url_list:
            attach_url = attach_name_and_url[1]
            attach_path = self._download_ydnote_url(
                file_path, attach_url, attach_name_and_url[0]
            )
            if not attach_path:
                continue
            # 将 attach_path 路径中 attachments 之前的路径去掉，只保留以 attachments 开头的之后的路径
            if self.is_relative_path:
                attach_path = attach_path[attach_path.find(ATTACH):]
            content = content.replace(attach_url, attach_path)

        with open(file_path, "wb") as f:
            f.write(content.encode())
        return

    def _download_image_as_base64(self, image_url) -> str:
        """
        下载图片并编码为 base64 data URI
        :param image_url: 图片 URL
        :return: data URI 字符串，失败返回空字符串
        """
        try:
            response = self.youdaonote_api.http_get(image_url)
        except requests.exceptions.ProxyError as err:
            logging.info("网络错误，下载图片「{}」失败: {}".format(image_url, format(err)))
            return ""

        content_type = response.headers.get("Content-Type")
        if response.status_code != 200 or not content_type:
            logging.info(
                "下载图片「{}」失败！图片可能已失效".format(image_url)
            )
            return ""

        # 从 Content-Type 或 URL 推断图片类型
        ext = self._get_image_extension(content_type, image_url)
        mime = MIME_MAP.get(ext, "image/png")

        try:
            img_data = base64.b64encode(response.content).decode("utf-8")
            return "data:{};base64,{}".format(mime, img_data)
        except Exception as err:
            logging.info("base64 编码图片「{}」失败: {}".format(image_url, format(err)))
            return ""

    @staticmethod
    def _get_image_extension(content_type: str, url: str) -> str:
        """从 Content-Type 或 URL 推断图片扩展名
        :param content_type: HTTP 响应的 Content-Type
        :param url: 图片 URL
        :return: 扩展名（如 .png）
        """
        # 先从 Content-Type 推断
        content_type_arr = content_type.split("/")
        if len(content_type_arr) == 2:
            ext = "." + content_type_arr[1].replace(";", "").strip().lower()
            if ext in MIME_MAP:
                return ext
            # 修正 jpeg -> .jpg
            if ext == ".jpeg":
                return ".jpeg"

        # 从 URL 路径推断
        url_path = urlparse(url).path
        ext = os.path.splitext(url_path)[1].lower()
        if ext in MIME_MAP:
            return ext

        return ".png"

    def _get_new_image_path(self, file_path, image_url) -> str:
        """
        将图片链接转换为新的链接
        :param file_path:
        :param image_url:
        :return: new_image_path
        """
        # 当 smms_secret_token 为空（不上传到 SM.MS），下载到图片到本地
        if not self.smms_secret_token:
            image_path = self._download_ydnote_url(file_path, image_url)
            return image_path or image_url

        # smms_secret_token 不为空，上传到 SM.MS
        new_file_url, error_msg = ImageUpload.upload_to_smms(
            youdaonote_api=self.youdaonote_api,
            image_url=image_url,
            smms_secret_token=self.smms_secret_token,
        )
        # 如果上传失败，仍下载到本地
        if not error_msg:
            return new_file_url
        logging.info(error_msg)
        image_path = self._download_ydnote_url(file_path, image_url)
        return image_path or image_url

    def _download_ydnote_url(self, file_path, url, attach_name=None) -> str:
        """
        下载文件到本地，返回本地路径
        :param file_path:
        :param url:
        :param attach_name:
        :return:  path
        """
        try:
            response = self.youdaonote_api.http_get(url)
        except requests.exceptions.ProxyError as err:
            error_msg = "网络错误，「{}」下载失败。错误提示：{}".format(url, format(err))
            logging.info(error_msg)
            return ""

        content_type = response.headers.get("Content-Type")
        file_type = "附件" if attach_name else "图片"
        if response.status_code != 200 or not content_type:
            error_msg = "下载「{}」失败！{}可能已失效，可浏览器登录有道云笔记后，查看{}是否能正常加载".format(
                url, file_type, file_type
            )
            logging.info(error_msg)
            return ""

        if attach_name:
            # 默认下载附件到 attachments 文件夹
            file_dirname = ATTACH
            file_suffix = attach_name
        else:
            # 默认下载图片到 images 文件夹
            file_dirname = IMAGES
            # 后缀 png 和 jpeg 后可能出现 ; `**.png;`, 原因未知
            content_type_arr = content_type.split("/")
            file_suffix = (
                "." + content_type_arr[1].replace(";", "")
                if len(content_type_arr) == 2
                else "jpg"
            )

        local_file_dir = None
        # 如果 file_name 中不包含 . 号
        if file_path.find(".") == -1:
            local_file_dir = os.path.join(self.root_local_dir or "", file_dirname).replace(
                "\\", "/"
            )
        else:
            # 截取字符串 file_path 中文件夹全路径(即实现在具体文件夹目录下再生成图片文件夹路径，而非在根目录生成图片文件夹路径)
            local_file_dir = os.path.join(
                file_path[: file_path.rfind("/")], file_dirname
            ).replace("\\", "/")

        if not os.path.exists(local_file_dir):
            os.mkdir(local_file_dir)
        file_basename = os.path.basename(urlparse(url).path)

        # 请求后的真实的 URL 中才有东西
        realUrl = parse.parse_qs(urlparse(response.url).query)

        if realUrl:
            filename = (
                realUrl.get("filename")[0]
                if realUrl.get("filename")
                else realUrl.get("download")[0]
                if realUrl.get("download")
                else ""
            )
            file_name = file_basename + filename
        else:
            file_name = "".join([file_basename, file_suffix])

        # 清理文件名中的特殊字符，防止 Markdown 链接截断
        file_name = self._sanitize_filename(file_name)

        local_file_path = os.path.join(local_file_dir, file_name).replace("\\", "/")

        try:
            with open(local_file_path, "wb") as f:
                f.write(response.content)  # response.content 本身就为字节类型
            logging.info("已将{}「{}」转换为「{}」".format(file_type, url, local_file_path))
        except:
            error_msg = "{} {}有误！".format(url, file_type)
            logging.info(error_msg)
            return ""

        return local_file_path

    def _set_relative_file_path(self, file_path, file_name, local_file_dir) -> str:
        """
        图片/附件设置为相对地址
        :param file_path:
        :param file_name:
        :param local_file_dir:
        :return:
        """
        note_file_dir = os.path.dirname(file_path)
        rel_file_dir = os.path.relpath(local_file_dir, note_file_dir)
        rel_file_path = os.path.join(rel_file_dir, file_name)
        new_file_path = rel_file_path.replace("\\", "/")
        return new_file_path


class ImageUpload(object):
    """
    图片上传到指定图床
    """

    @staticmethod
    def upload_to_smms(youdaonote_api, image_url, smms_secret_token) -> Tuple[str, str]:
        """
        上传图片到 sm.ms
        :param image_url:
        :param smms_secret_token:
        :return: url, error_msg
        """
        try:
            smfile = youdaonote_api.http_get(image_url).content
        except:
            error_msg = "下载「{}」失败！图片可能已失效，可浏览器登录有道云笔记后，查看图片是否能正常加载".format(image_url)
            return "", error_msg
        files = {"smfile": smfile}
        upload_api_url = "https://sm.ms/api/v2/upload"
        headers = {"Authorization": smms_secret_token}

        error_msg = (
            "SM.MS 免费版每分钟限额 20 张图片，每小时限额 100 张图片，大小限制 5 M，上传失败！「{}」未转换，"
            "将下载图片到本地".format(image_url)
        )
        try:
            res_json = requests.post(
                upload_api_url, headers=headers, files=files, timeout=5
            ).json()
        except requests.exceptions.ProxyError as err:
            error_msg = "网络错误，上传「{}」到 SM.MS 失败！将下载图片到本地。错误提示：{}".format(
                image_url, format(err)
            )
            return "", error_msg
        except Exception:
            return "", error_msg

        if res_json.get("success"):
            url = res_json["data"]["url"]
            logging.info("已将图片「{}」转换为「{}」".format(image_url, url))
            return url, ""
        if res_json.get("code") == "image_repeated":
            url = res_json["images"]
            logging.info("已将图片「{}」转换为「{}」".format(image_url, url))
            return url, ""
        if res_json.get("code") == "flood":
            return "", error_msg

        error_msg = (
            "上传「{}」到 SM.MS 失败，请检查图片 url 或 smms_secret_token（{}）是否正确！将下载图片到本地".format(
                image_url, smms_secret_token
            )
        )
        return "", error_msg
