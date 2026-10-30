"""消息文本规则：占位符词表、引用标记、@昵称。

入站清洗、出站拦截与正文发送共用同一份词表——多写一份就会漏，漏一个括号就等于把脏文本发给用户。
"""

from __future__ import annotations

import json
import re

# 模型偶尔把媒体写成方括号占位符进正文；这些不是媒体，发送前拦截。
# 词表要覆盖模型自己编出来的叫法（[语音克隆] / [表情包] …）：漏一个就等于把方括号发给用户，
# 而且这条脏文本会入库，被后续历史喂回模型，越用越像"这里的规矩就是这样"
_MEDIA_WORDS = (
    r"表情|表情包|语音|语音克隆|语音条|克隆语音|图片|视频|文件|邮件"
    r"|emoji|emoticon|sticker|voice|audio|image|photo|video|file|mail"
)
# 不接话也是一个动作（调 silence），但模型会把它写成正文里的标志（[SILENCE] / [沉默]）：
# 一起收进同一份词表，才不会发出去、也不会入库喂回历史
_SILENCE_WORDS = r"沉默|不说话|不接话|无话可说|silence|silent"
_PLACEHOLDER_WORDS = rf"{_MEDIA_WORDS}|{_SILENCE_WORDS}"


def _token(words: str) -> str:
    """占位标志的形状：中英方/圆括号 + 可选 8 位短标识 + 可选紧跟的（描述）。"""
    return (
        rf"[\[【(（]\s*(?:{words})(?:\s+[0-9a-fA-F]{{6,8}})?\s*[\]】)）]"
        r"(?:\s*[（(][^）)]{0,120}[）)])?"
    )


# 入站富媒体在上下文里就长成 `[图片]（描述）` / `[语音 6ce72460]` 这样，模型照抄的是整套形状：
# 括号里的 8 位短标识与紧跟的（描述）一起吞掉，否则余下一截孤立括号更像"这里允许写"
_MEDIA_TOKEN = _token(_PLACEHOLDER_WORDS)
PLACEHOLDER_RE = re.compile(
    rf"^(?:\s*{_MEDIA_TOKEN}\s*[。.!！?？~～…]*\s*)+$",
    re.IGNORECASE,
)
# 同一类占位符夹在正常文字里（“哈哈[表情]”）时直接剔除，避免用户看到方括号
PLACEHOLDER_INLINE_RE = re.compile(_MEDIA_TOKEN, re.IGNORECASE)
# 整条正文只是沉默标志（可夹标点）——意图是这一轮不接话，按 silence 同等处理
SILENCE_RE = re.compile(
    rf"^(?:\s*{_token(_SILENCE_WORDS)}\s*[。.!！?？~～…]*\s*)+$",
    re.IGNORECASE,
)
# 正文开头的引用标记：[引用 关键词] / 【引用】；允许前面先写 @昵称（head 保留，不参与正文剥离）
QUOTE_RE = re.compile(
    r"^\s*(?P<head>(?:@[\w\u4e00-\u9fff·._-]{1,32}\s*)*)"
    r"[\[【]\s*引用(?:\s*[:：]?\s*([^\]】\n]*?))?\s*[\]】]\s*"
)
# @昵称：允许中文、字母数字、下划线与常见连接符，避免把邮箱/代码里的 @ 当成人
AT_NAME_RE = re.compile(r"(?<![\w@])@([\w\u4e00-\u9fff·._-]{1,32})")


def to_str(v) -> str:
    if isinstance(v, str):
        return v
    return json.dumps(v, ensure_ascii=False)
