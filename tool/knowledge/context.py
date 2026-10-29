"""会话上下文清理：心腹及以上可以在对话里让机器人忘掉本会话。"""

from __future__ import annotations

import asyncio

from ..base import Tool


class ClearContext(Tool):
    name = "clear_context"
    min_level = 4
    data_name = "knowledge"
    description = (
        "清空当前会话的上下文：删除本会话的全部消息与历史摘要，长期记忆不受影响。"
        "只在对方明确要求清空上下文、忘掉刚才聊的、重新开始、不要留着这些记录时调用；"
        "只能清空自己所在的这个会话，清不掉别的会话。"
    )
    parameters = {"type": "object", "properties": {}}
    prompt_hint = (
        "对方说“清空上下文”“把刚才聊的忘掉”“重新开始”时调 clear_context，"
        "按返回的条数如实告知；只是问“还记不记得某事”用 memory_search，不要清上下文。"
    )

    async def run(self, args: dict, ctx: dict) -> dict:
        actor = ctx.get("actor") or {}
        # 门槛可能在工具页被下调，执行期再按发送者等级校验一次
        if int(actor.get("level") or 0) < self.min_level:
            return {"ok": False, "error": f"清空上下文需要心腹（L{self.min_level}）"}
        sid = str(ctx.get("session_id") or "").strip()
        if not sid:
            return {"ok": False, "error": "缺少会话标识，无法清空上下文"}
        removed = await asyncio.to_thread(self.rt.storage.clear_session_context, sid)
        cleared = int(removed.get("messages") or 0)
        return {
            "ok": True,
            "cleared": cleared,
            "note": f"本会话上下文已清空：{cleared} 条消息，历史摘要已重置（长期记忆保留）",
        }


def get_tools(rt):
    return [ClearContext(rt)]
