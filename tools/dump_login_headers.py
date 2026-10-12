# -*- coding: utf-8 -*-
"""诊断：cookies.json 登录后打印请求头/响应 Set-Cookie/cookie 罐，确认能否拿到 P_INFO。

用法（项目根目录执行）：
    python tools/dump_login_headers.py [cookies.json 路径]
不传路径则用默认位置。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.exporter import Exporter


def _shorten(text, limit=300):
    text = text or ""
    return text[:limit] + (" ...(截断)" if len(text) > limit else "")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    exporter = Exporter()

    err = exporter.login_from_file(path)
    print("login_from_file 结果:", err or "成功(无错误)")
    if err:
        return 1

    api = exporter.api
    resp = api.http_post(
        api.ROOT_ID_URL.format(cstk=api.cstk),
        data={"path": "/", "entire": "true", "purge": "false", "cstk": api.cstk},
    )

    print("\n=== 1. 我们发出的请求头 ===")
    for key, value in resp.request.headers.items():
        print("{}: {}".format(key, _shorten(value)))
    cookie_header = resp.request.headers.get("Cookie", "")
    print("\nCookie 是否含 P_INFO:", "P_INFO" in cookie_header)

    print("\n=== 2. 服务器响应头（重点 Set-Cookie） ===")
    set_cookies = []
    for key, value in resp.headers.items():
        if key.lower() == "set-cookie":
            set_cookies.append(value)
            print("Set-Cookie:", _shorten(value))
    if not set_cookies:
        print("(响应中没有 Set-Cookie，服务器未回传任何 cookie)")
    print("响应是否回传 P_INFO:", any("P_INFO" in v for v in set_cookies))

    print("\n=== 3. 请求后 cookie 罐 ===")
    for cookie in api.session.cookies:
        print(" -", cookie.name)

    print("\n=== 4. 解析出的真实账号 ===")
    print(repr(api.get_real_account()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
