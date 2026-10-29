"""沉默：把「这轮不接话」变成一个可以主动调用的动作。"""

from __future__ import annotations

from ..base import Tool


class Silence(Tool):
    name = "silence"
    description = (
        "本轮什么都不发，安静结束。适合不接的情况：话不是冲你来、群里在聊你自己的话题、"
        "对方只是随口一句、刚说过同样的意思、不值得回应。"
        "不想接就调它，不要为了应付硬凑一句，也不要只回一段文本"
    )
    parameters = {
        "type": "object",
        "properties": {"reason": {"type": "string",
                                  "description": "可选：一两个词说明为什么不接，只进日志不发给任何人"}},
    }
    prompt_hint = ("不是每轮都必须说话：不值得接就调 silence，别硬凑一句，"
                   "也别只回文本（那会被当成话发出去）。")

    async def run(self, args: dict, ctx: dict) -> dict:
        ctx["silence"] = True
        return {"ok": True}


def get_tools(rt):
    return [Silence(rt)]
