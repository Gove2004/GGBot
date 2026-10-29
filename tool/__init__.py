"""Tool 执行器与 agent 循环。

一切能力都是 Tool：插件按能力域放在项目根 tool/{chat,knowledge,fs,mail,admin}/，
启动时自动发现并按配置挂载 schema。每个 toolset 目录自包含代码、config.json 与持久化数据。
每个 Tool 带 min_level 门槛：请求只下发当前发送者能用的 schema，执行器再按实际发送者校验。
回合语义：优先通过工具行动——send_text 发送消息；未调用任何工具即本轮不回复。
模型无视协议直接回文本且本轮尚未发送时，自动按 send_text 兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging

from tool.base import PLACEHOLDER_RE, ToolRegistry, ToolRuntime
from ggbot.core.runtime_stats import runtime
from ggbot.util.session import oid8, session_label

logger = logging.getLogger("ggbot.tools")

NO_REPLY = "__NO_REPLY__"

PLACEHOLDER_HINT = (
    "不要用 [表情]/[语音]/[图片] 这类方括号占位符：它们只是文字，不会变成媒体。"
    "要发表情就调用 send_sticker，发语音就调用 send_voice_clone，发图/视频就调用 "
    "send_image/send_video。"
)


def _actor_label(actor: dict) -> str:
    name = (actor.get("name") or "").strip()
    oid = oid8(actor.get("openid"))
    return f"{name}/{oid}" if name and oid else (name or oid or "unknown")


class ToolExecutor:
    """执行 function calling 调用。ctx 提供回复目标、会话上下文与发送者权限。"""

    def __init__(self, cfg, storage, memory, bot):
        self.rt = ToolRuntime(cfg, storage, memory, bot)
        self.registry = ToolRegistry(self.rt)
        counts: dict[str, int] = {}
        for t in self.registry.tools.values():
            counts[t.category] = counts.get(t.category, 0) + 1
        summary = " / ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        logger.debug("工具就绪: %d 个（%s）", len(self.registry.tools), summary or "无")

    def schemas(self, level: int) -> list[dict]:
        return self.registry.schemas(level)

    def hints(self, level: int) -> str:
        return self.registry.hints(level)

    def list_tools(self) -> list[dict]:
        return self.registry.list_info()

    def toggle(self, name: str, enabled: bool) -> dict | None:
        return self.registry.toggle(name, enabled)

    def set_level(self, name: str, level: int) -> dict | None:
        return self.registry.set_level(name, level)

    async def execute(self, name: str, args: dict, ctx: dict) -> dict:
        tool = self.registry.tools.get(name)
        if tool is None:
            return {"ok": False, "error": f"未知工具 {name}"}
        runtime.tool()
        if not tool.is_enabled():
            return {"ok": False, "error": f"工具 {name} 未启用"}
        actor = ctx.get("actor") or {}
        level = int(actor.get("level") or 0)
        need = tool.required_level()
        sid = session_label(ctx.get("session_id"))
        if need > 5 or level < need:
            need_label = "永久锁" if need > 5 else f"L{need}"
            logger.warning("工具 %s 拒绝: %s(L%d) 需 %s", name, _actor_label(actor), level,
                           need_label, extra={"session": sid})
            return {"ok": False, "error": f"权限不足：{name} 需要 {need_label}，当前 L{level}"}
        if need >= 3:
            logger.info("工具 %s 允许: %s(L%d)", name, _actor_label(actor), level,
                        extra={"session": sid})
        else:
            logger.debug("工具 %s args=%s actor=%s(L%d)", name,
                         json.dumps(args, ensure_ascii=False)[:200], _actor_label(actor), level,
                         extra={"session": sid})
        try:
            return await tool.run(args, ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception("工具 %s 执行失败", name, extra={"session": sid})
            ctx["tool_error"] = f"{name}: {e}"
            return {"ok": False, "error": str(e)}


async def agent_loop(llm, executor, messages: list, ctx: dict, max_rounds: int = 6) -> str:
    """function calling 循环：LLM 决策 → 执行工具 → 结果回传 → 继续，直到结束回合。

    回合结束条件（返回 NO_REPLY，调用方不会把文本当回复发送）：
    - LLM 未调用工具但有文本，且本轮尚未发送过 → 按 send_text 兜底发送；
    - LLM 未调用工具且无文本（或已有发送）→ 结束本轮，文本忽略；
    - 达到 max_rounds。
    回复内容优先由工具发送（send_text / send_image 等），不依赖返回文本。
    """
    level = int((ctx.get("actor") or {}).get("level") or 0)
    tools = executor.schemas(level)
    if not tools:
        logger.warning("没有可用的工具（等级 L%d），跳过本轮", level)
        return NO_REPLY
    history = list(messages)
    base_len = max(0, len(messages) - 1)
    retried_placeholder = False
    for round_no in range(max_rounds):
        resp = await asyncio.to_thread(
            llm.chat, history, tools, base_len=base_len, call_no=round_no + 1
        )
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            content = (msg.content or "").strip()
            if content and PLACEHOLDER_RE.match(content):
                logger.warning("模型用占位符代替工具调用: %s", content)
                if not retried_placeholder and round_no + 1 < max_rounds:
                    retried_placeholder = True
                    history.append({"role": "assistant", "content": content})
                    history.append({"role": "system", "content": PLACEHOLDER_HINT})
                    continue
                return NO_REPLY
            # 模型偶尔无视工具协议直接回文本：本轮还没发过就兜底发出去，避免整轮静默
            if content and not ctx.get("sent_any"):
                logger.debug("模型未调用工具，默认 send_text")
                await executor.execute("send_text", {"content": content}, ctx)
            else:
                logger.debug("agent 未调用工具，结束本轮")
            return NO_REPLY
        logger.debug("agent 工具调用: %s", ", ".join(tc.function.name for tc in calls))
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
    # 工具循环超限：兜底结束（不回复）
    return NO_REPLY
