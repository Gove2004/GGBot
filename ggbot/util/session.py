"""会话显示标签：场景前缀 + 8 位 openid（不解析真名，避免白名单/网络依赖）。"""

from __future__ import annotations

SYSTEM_LABEL = "系统00000000"


def oid8(oid: str | None) -> str:
    """openid 前 8 位（日志/面板展示用）。"""
    return (oid or "")[:8]


def session_label(sid: str | None) -> str:
    """session key（group:<gid> / c2c:<openid>）→ 群聊xxxxxxxx / 私聊xxxxxxxx / 系统00000000。"""
    prefix, sep, rest = (sid or "").partition(":")
    if sep and prefix == "group":
        return "群聊" + oid8(rest)
    if sep and prefix == "c2c":
        return "私聊" + oid8(rest)
    raw = oid8(rest if sep else sid)
    if not raw or raw.lower() == "system":
        return SYSTEM_LABEL
    return "系统" + raw
