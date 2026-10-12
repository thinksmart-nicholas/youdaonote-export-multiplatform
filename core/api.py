import json
import re

import requests

from core.common import resolve_data_file


class YoudaoNoteApi(object):
    """
    有道云笔记 API 封装
    原理：https://depp.wang/2020/06/11/how-to-find-the-api-of-a-website-eg-note-youdao-com/
    """

    ROOT_ID_URL = "https://note.youdao.com/yws/api/personal/file?method=getByPath&keyfrom=web&cstk={cstk}"
    DIR_MES_URL = (
        "https://note.youdao.com/yws/api/personal/file/{dir_id}?all=true&f=true&len=1000&sort=1"
        "&isReverse=false&method=listPageByParentId&keyfrom=web&cstk={cstk}"
    )
    FILE_URL = (
        "https://note.youdao.com/yws/api/personal/sync?method=download&_system=macos&_systemVersion=&"
        "_screenWidth=1280&_screenHeight=800&_appName=ynote&_appuser=0123456789abcdeffedcba9876543210&"
        "_vendor=official-website&_launch=16&_firstTime=&_deviceId=0123456789abcdef&_platform=web&"
        "_cityCode=110000&_cityName=&sev=j1&keyfrom=web&cstk={cstk}"
    )

    def __init__(self, cookies_path=None):
        """
        初始化
        :param cookies_path:
        """
        self.session = requests.session()  # 使用 session 维持有道云笔记的登陆状态
        self.session.headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/100.0.4896.88 Safari/537.36",
            "Accept": "*/*",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="100", "Google Chrome";v="100"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        }
        self.cookies_path = (
            cookies_path
            if cookies_path
            else resolve_data_file("cookies.json")
        )
        self.cstk = None

    def set_cookies(self, cookies) -> str:
        """
        把 cookies 装载到 Session，并提取 cstk。

        与 `login_by_cookies` 的区别：本方法直接接收 cookies 列表，
        供界面（登录表单）等「不落 cookies.json 文件」的场景使用。

        :param cookies: [[name, value, domain, path], ...]
        :return: error_msg，空字符串表示成功
        """
        if not cookies:
            return "没有 cookies，请重新抓取后重试"

        for cookie in cookies:
            self.session.cookies.set(
                name=cookie[0], value=cookie[1], domain=cookie[2], path=cookie[3]
            )

        # cstk 用于请求时接口验证。
        # YNOTE_CSTK 不一定排在 cookies 的第一位，必须全量查找，
        # 否则明明存在该字段也会被误判为空。
        self.cstk = None
        for cookie in cookies:
            if cookie and cookie[0] == "YNOTE_CSTK":
                self.cstk = cookie[1]
                break
        if not self.cstk:
            return "YNOTE_CSTK 字段为空，请检查「cookies.json」中是否包含 YNOTE_CSTK"
        return ""

    def _covert_cookies(self) -> list:
        """
        读取 cookies 文件的 cookies，并转换为字典
        :return: cookies
        """
        with open(self.cookies_path, "rb") as f:
            json_str = f.read().decode("utf-8")

        try:
            cookies_dict = json.loads(json_str)  # 将字符串转换为字典
            cookies = cookies_dict["cookies"]
        except Exception:
            raise Exception("转换「{}」为字典时出现错误".format(self.cookies_path))
        return cookies

    def login_by_cookies(self) -> str:
        """
        使用 Cookies 登录：从 cookies.json 读取后装载到 Session
        :return: error_msg
        """
        try:
            cookies = self._covert_cookies()
        except Exception as err:
            return format(err)
        if not cookies:
            return "「cookies.json」中没有 cookies，请重新抓取后重试"
        return self.set_cookies(cookies)

    def http_post(self, url, data=None, files=None):
        """
        封装 post 请求
        :param url:
        :param data:
        :param files:
        :return: response
        """
        return self.session.post(url, data=data, files=files)

    def http_get(self, url):
        """
        封装 get 请求
        :param url:
        :return: response
        """
        return self.session.get(url)

    def get_root_dir_info_id(self) -> dict:
        """
        获取有道云笔记根目录信息
        :return: {
            'fileEntry': {'id': 'test_root_id', 'name': 'ROOT', ...},
            ...
        }
        """
        data = {"path": "/", "entire": "true", "purge": "false", "cstk": self.cstk}
        return self.http_post(self.ROOT_ID_URL.format(cstk=self.cstk), data=data).json()

    def get_dir_info_by_id(self, dir_id) -> dict:
        """
        根据目录 ID 获取目录下所有文件信息
        :return: {
            'count': 3,
            'entries': [
                 {'fileEntry': {'id': 'test_dir_id', 'name': 'test_dir', 'dir': true, ...}},
                 {'fileEntry': {'id': 'test_note_id', 'name': 'test_note', 'dir': false, ...}}
                 ...
            ]
        }
        """
        url = self.DIR_MES_URL.format(dir_id=dir_id, cstk=self.cstk)
        return self.http_get(url).json()

    def get_file_by_id(self, file_id):
        """
        根据文件 ID 获取文件内容
        :param file_id:
        :return: response，内容为笔记字节码
        """
        data = {
            "fileId": file_id,
            "version": -1,
            "convert": "true",
            "editorType": 1,
            "cstk": self.cstk,
        }
        url = self.FILE_URL.format(cstk=self.cstk)
        return self.http_post(url, data=data)

    #: 查询当前登录账号真实邮箱的接口（抓包实测确认存在，cookie 可直接访问）
    SELF_URL = "https://note.youdao.com/yws/api/self?method=get&keyfrom=web&cstk={cstk}"

    #: 账号识别：优先邮箱，其次手机号
    EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    PHONE_RE = re.compile(r"^\+?\d{6,20}$")

    def _match_account(self, text):
        """从字符串中识别邮箱（优先）或手机号，都不是则返回空串。"""
        text = (text or "").strip()
        m = self.EMAIL_RE.search(text)
        if m:
            return m.group(0)
        if self.PHONE_RE.match(text):
            return text
        return ""

    def _extract_account(self, obj):
        """递归扫描接口返回的任意 JSON 结构，找出第一个邮箱/手机号。"""
        if isinstance(obj, dict):
            # 先按常见字段名快速命中（按可靠性排序）：
            # - objectUser：实测确认，有道即使返回错误（如 500 bad uri）也会带上真实账号
            # - name：/yws/api/self 返回的用户名字段
            # 未命中时下面的递归扫描仍会遍历所有值，兜底识别邮箱/手机号
            for key in (
                "email",
                "objectUser",
                "mail",
                "username",
                "loginName",
                "account",
                "user",
                "name",
            ):
                val = obj.get(key)
                if isinstance(val, str):
                    hit = self._match_account(val)
                    if hit:
                        return hit
            for val in obj.values():
                hit = self._extract_account(val)
                if hit:
                    return hit
        elif isinstance(obj, (list, tuple)):
            for val in obj:
                hit = self._extract_account(val)
                if hit:
                    return hit
        elif isinstance(obj, str):
            return self._match_account(obj)
        return ""

    def get_account_from_self_api(self) -> str:
        """
        调用 /yws/api/self 查询当前登录账号的真实邮箱。

        该接口为抓包实测确认存在的接口，cookie 可直接访问，返回 name 等字段；
        若命中错误响应（500），其中 objectUser 也会带上真实账号。
        取不到时返回空串。
        """
        if not self.cstk:
            return ""
        try:
            resp = self.http_get(self.SELF_URL.format(cstk=self.cstk))
            data = resp.json()
        except Exception:
            return ""
        if not isinstance(data, dict):
            return ""
        return self._extract_account(data)

    def get_real_account(self) -> str:
        """
        返回当前 cookies 真正所属的有道云笔记账号（邮箱）。

        多来源依次尝试：
        1. /yws/api/self 接口（抓包实测确认存在，返回真实邮箱）；
        2. P_INFO cookie：「|」分隔的第一项就是真实账号（如 xxx@xx.com|...）；
        3. YNOTE_LOGIN cookie（有道通常存的就是登录邮箱，需 URL 解码）；
        4. 扫描全部 cookie 解码后提取邮箱。
        都取不到返回空串。
        """
        import urllib.parse

        # 1) /yws/api/self 接口
        try:
            hit = self.get_account_from_self_api()
            if hit:
                return hit
        except Exception:
            pass

        # 2) P_INFO：第一项即真实账号，形如 "xxx@xx.com|1699999999|1"
        try:
            p_info = self.session.cookies.get("P_INFO")
            if p_info:
                first = urllib.parse.unquote(p_info).split("|")[0].strip()
                if first:
                    return self._match_account(first) or first
        except Exception:
            pass

        # 3) YNOTE_LOGIN cookie
        try:
            login_cookie = self.session.cookies.get("YNOTE_LOGIN")
            if login_cookie:
                hit = self._match_account(urllib.parse.unquote(login_cookie))
                if hit:
                    return hit
        except Exception:
            pass

        # 4) 扫描全部 cookie
        try:
            for cookie in self.session.cookies:
                hit = self._match_account(urllib.parse.unquote(cookie.value or ""))
                if hit:
                    return hit
        except Exception:
            pass

        try:
            names = [c.name for c in self.session.cookies]
            print("[api] 未能从接口和 cookie 中确定账号；当前 cookie：", names)
        except Exception:
            pass
        return ""
