# -*- coding: utf-8 -*-
"""账户 Cookie 的本地持久化（SQLite）。

表结构（与 HANDOFF 中 Phase 2 登录页需求一致）：

    accounts(user_name TEXT PRIMARY KEY,
             cstk TEXT NOT NULL,
             login TEXT NOT NULL,
             sess TEXT NOT NULL)

其中 ``user_name`` 是主键（唯一字符串，区分不同账户）；
``cstk`` / ``login`` / ``sess`` 三列各自存成 **cookies.json 里单条 cookie 的 JSON 文本**，
即 ``[name, value, domain, path]`` 形式（例如 ``["YNOTE_CSTK", "xxxx", ".note.youdao.com", "/"]``）。

读取时把三列拼成一个 ``[[...],[...],[...]]`` 列表，即可直接交给
``Exporter.login(cookies)`` 使用，与命令行 cookies.json 完全等价。
"""
import json
import os
import sqlite3

from core.common import resolve_data_dir

#: 登录必需的三个 cookie 字段
REQUIRED_COOKIE_NAMES = ("YNOTE_CSTK", "YNOTE_LOGIN", "YNOTE_SESS")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    user_name TEXT PRIMARY KEY,
    cstk TEXT NOT NULL,
    login TEXT NOT NULL,
    sess TEXT NOT NULL
)
"""


def _db_path():
    data_dir = resolve_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "accounts.db")


def _connect():
    conn = sqlite3.connect(_db_path())
    conn.execute(_SCHEMA)
    return conn


def get_cookies(user_name):
    """返回该用户名保存的 cookies 列表 ``[[name, value, domain, path], ...]``，
    未找到返回 ``None``。
    """
    if not user_name:
        return None
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT cstk, login, sess FROM accounts WHERE user_name = ?",
            (user_name,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return [json.loads(row[0]), json.loads(row[1]), json.loads(row[2])]


def save_cookies(user_name, cookies):
    """把一次成功登录得到的 cookies 存入数据库（已存在则覆盖）。

    :param cookies: ``[[name, value, domain, path], ...]``
    :raises ValueError: 缺少 YNOTE_CSTK / YNOTE_LOGIN / YNOTE_SESS 任一项
    """
    if not user_name:
        raise ValueError("用户名不能为空")
    if not cookies:
        raise ValueError("没有可保存的 Cookie")

    mapping = {item[0]: item for item in cookies}
    missing = [name for name in REQUIRED_COOKIE_NAMES if name not in mapping]
    if missing:
        raise ValueError("Cookie 缺少字段：{}".format("、".join(missing)))

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO accounts(user_name, cstk, login, sess) VALUES(?,?,?,?) "
            "ON CONFLICT(user_name) DO UPDATE SET "
            "cstk=excluded.cstk, login=excluded.login, sess=excluded.sess",
            (
                user_name,
                json.dumps(mapping["YNOTE_CSTK"]),
                json.dumps(mapping["YNOTE_LOGIN"]),
                json.dumps(mapping["YNOTE_SESS"]),
            ),
        )
        conn.commit()
    finally:
        conn.close()
