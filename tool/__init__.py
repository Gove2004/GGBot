"""Tool 执行器与 agent 循环。

一切能力都是 Tool：插件按能力域放在项目根 tool/{core,memory,emoji,audio,media,mail}/，
启动时自动发现并按配置挂载 schema。每个 toolset 目录自包含代码、config.json 与持久化数据。
回合语义：LLM 的纯文本输出不会作为回复发送（会被忽略）；必须通过工具调用行动——
send_text/seed_message 发送消息、end_call 结束本轮（或未调用任何工具时默认视为 end_call）。
"""

from __future__ import annotations

import asyncio
import json
import logging

from tool.base import ToolRegistry, ToolRuntime
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools")

NO_REPLY = "__NO_REPLY__"


class ToolExecutor:
    """执行 function calling 调用。ctx 提供回复目标与会话上下文。"""

    def __init__(self, cfg, storage, memory, bot):
        self.rt = ToolRuntime(cfg, storage, memory, bot)
        self.registry = ToolRegistry(self.rt)
        counts: dict[str, int] = {}
        for t in self.registry.tools.values():
            counts[t.category] = counts.get(t.category, 0) + 1
        summary = " / ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        logger.debug("工具就绪: %d 个（%s）", len(self.registry.tools), summary or "无")

    @property
    def schemas(self) -> list[dict]:
        return self.registry.schemas()

    def hints(self) -> str:
        return self.registry.hints()

    def list_tools(self) -> list[dict]:
        return self.registry.list_info()

    def toggle(self, name: str, enabled: bool) -> dict | None:
        return self.registry.toggle(name, enabled)

    async def execute(self, name: str, args: dict, ctx: dict) -> dict:
        tool = self.registry.get(name)
        if tool is None:
            known = name in self.registry.tools
            return {"ok": False, "error": f"工具 {name} 未启用" if known else f"未知工具 {name}"}
        sid = session_label(ctx.get("session_id"))
        if name not in ("send_text", "seed_message", "end_call"):
            logger.debug("工具 %s args=%s", name, json.dumps(args, ensure_ascii=False)[:200],
                         extra={"session": sid})
        try:
            return await tool.run(args, ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception("工具 %s 执行失败", name, extra={"session": sid})
            if name.startswith(("send_", "seed_")):
                ctx["send_error"] = str(e)  # 发送类工具失败标记，供回合收尾区分 skipped/failed
            return {"ok": False, "error": str(e)}


async def agent_loop(llm, executor, messages: list, ctx: dict, max_rounds: int = 6) -> str:
    """function calling 循环：LLM 决策 → 执行工具 → 结果回传 → 继续，直到结束回合。

    回合结束条件（返回 NO_REPLY，调用方不会把文本当回复发送）：
    - LLM 某轮未调用任何工具 → 默认 end_call，该轮纯文本被忽略；
    - LLM 显式调用 end_call → 立即结束；
    - 达到 max_rounds。
    回复内容由工具发送（send_text / seed_message / send_image 等），不依赖返回文本。
    """
    tools = executor.schemas
    if not tools:
        logger.warning("没有启用的工具，跳过本轮")
        return NO_REPLY
    history = list(messages)
    for _round in range(max_rounds):
        resp = await asyncio.to_thread(llm.chat, history, tools)
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            # 未调用工具 = 默认 end_call；纯文本内容忽略（不发送）
            logger.debug("agent 未调用工具，默认 end_call（文本忽略）: %.60r", (msg.content or "")[:60])
            return NO_REPLY
        names = [tc.function.name for tc in calls]
        history.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in calls
        ]})
        for tc in calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await executor.execute(tc.function.name, args, ctx)
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, ensure_ascii=False),
            })
        if "end_call" in names:
            logger.debug("agent 显式 end_call 结束本轮")
            return NO_REPLY
    # 工具循环超限：兜底结束（不回复）
    return NO_REPLY
