# -*- coding: utf-8 -*-
"""探测有道「用户信息」接口，找出哪个能返回真实账号（邮箱/手机号）。

接口来源说明：
- /yws/api/self    ：抓包实测确认存在的接口（博客园《有道云笔记接口分析》），
                     返回 name 字段即用户名，cookie 可直接访问。
- /yws/open/user/get.json ：官方 OpenAPI，返回 user 字段为邮箱，
                     但需要 OAuth（oauth_token 等），仅有 cookie 时预计失败，
                     列出仅为验证这一点。
- 其余为历史候选，保留用于对比。

用法（项目根目录执行）：
    python tools/probe_user_endpoints.py [cookies.json 路径]

不传路径时使用默认位置 core.common.resolve_data_file("cookies.json")。
"""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.exporter import Exporter

ENDPOINTS = (
    # 1. 抓包实测确认存在的接口（最可能是正解）
    "https://note.youdao.com/yws/api/self?method=get&keyfrom=web&cstk={cstk}",
    # 2. 官方 OpenAPI（需 OAuth，预计失败，仅验证）
    "https://note.youdao.com/yws/open/user/get.json",
    # 3. 历史候选
    "https://note.youdao.com/yws/api/user?method=get&keyfrom=web&cstk={cstk}",
    "https://note.youdao.com/yws/mapi/user?method=get&keyfrom=web&cstk={cstk}",
    "https://note.youdao.com/yws/api/personal/user?method=get&keyfrom=web&cstk={cstk}",
    "https://note.youdao.com/yws/api/user/profile?method=get&keyfrom=web&cstk={cstk}",
)


def _shorten(text, limit=600):
    text = text or ""
    return text[:limit] + (" ...(截断)" if len(text) > limit else "")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else None
    exporter = Exporter()

    err = exporter.login_from_file(path)
    print("登录:", err or "成功(无错误)")
    if err:
        return 1

    api = exporter.api
    print("cstk 是否已取到:", bool(api.cstk))
    print("=" * 70)

    for tpl in ENDPOINTS:
        url = tpl.format(cstk=api.cstk)
        print("\n[GET] {}".format(url))
        try:
            resp = api.http_get(url)
        except Exception as ex:
            print("  请求异常:", ex)
            continue

        print("  状态码: {}   Content-Type: {}".format(
            resp.status_code, resp.headers.get("Content-Type", "-")))
        body = resp.text
        print("  响应体: {}".format(_shorten(body, 600)))

        # 500 响应里可能藏 objectUser，单独提示
        obj_user = ""
        try:
            data = resp.json()
        except Exception:
            data = None
        if isinstance(data, dict):
            obj_user = data.get("objectUser") or ""
            if obj_user:
                print("  >>> 响应含 objectUser: {!r}".format(obj_user))

        account = ""
        try:
            account = api._extract_account(resp.json())
        except Exception:
            try:
                m = api.EMAIL_RE.search(body)
                account = m.group(0) if m else ""
            except Exception:
                account = ""
        print("  >>> 提取到的账号: {!r}".format(account))

    print("\n" + "=" * 70)
    print("当前 cookie 罐:", [c.name for c in api.session.cookies])
    print("get_real_account():", repr(api.get_real_account()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
