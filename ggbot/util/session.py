"""会话显示标签：场景前缀 + 8 位 openid（不解析真名，避免白名单/网络依赖）。"""

from __future__ import annotations


def oid8(oid: str | None) -> str:
    """openid 前 8 位（日志/面板展示用）。"""
    return (oid or "")[:8]


def session_label(sid: str | None) -> str:
    """session key（group:<gid> / c2c:<openid>）→ 群xxxxxxxx / 私聊xxxxxxxx。"""
    prefix, _, rest = (sid or "").partition(":")
    if prefix == "group":
        return "群" + oid8(rest)
    if prefix == "c2c":
        return "私聊" + oid8(rest)
    return oid8(sid)
