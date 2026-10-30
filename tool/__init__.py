"""Tool 执行器与 agent 循环。

一切能力都是 Tool：插件按能力域放在项目根 tool/{core,media,memory,web,fs,mail,admin}/，
启动时自动发现并按配置挂载 schema。每个 toolset 目录自包含代码、config.json 与持久化数据。
每个 Tool 带 min_level 门槛：全部启用的工具都下发给模型（不按等级裁剪），执行器按实际发送者校验，权限不足直接失败。
回合语义：模型只有一条说话通道——回复正文，由 ggbot.core.outbound 逐行拆成消息发出（正文不是工具，模型无从调用）；
媒体、沉默等才走工具（send_sticker / send_voice_clone / silence …），silence 主动不接话。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

from ggbot.core.outbound import Outbound
from ggbot.core.runtime_stats import runtime
from ggbot.util.logging_setup import clip
from ggbot.util.session import oid8, session_label
from ggbot.util.text import PLACEHOLDER_RE, to_str
from tool.base import ToolRegistry, ToolRuntime

logger = logging.getLogger("ggbot.tools")

NO_REPLY = "__NO_REPLY__"

PLACEHOLDER_HINT = (
    "正文里写媒体占位词是无效的：方括号也好、圆括号也好，写出来只是文字，不会变成图片/语音/表情。"
    "要发媒体就直接调用对应工具（send_sticker / send_voice_clone / send_image / send_video），"
    "只说一句人话即可。"
)


def _actor_label(actor: dict) -> str:
    name = (actor.get("name") or "").strip()
    oid = oid8(actor.get("openid"))
    return f"{name}/{oid}" if name and oid else (name or oid or "unknown")


ARG_TOTAL_LIMIT = 200
ARG_VALUE_LIMIT = 120


def _args_brief(args: dict) -> str:
    """工具参数一行展示：k=v 逗号分隔，长值与总长度截断（含密钥的参数不应出现在 schema 里）。"""
    parts = []
    for k, v in (args or {}).items():
        flat = re.sub(r"\s+", " ", to_str(v))
        parts.append(f"{k}={clip(flat, ARG_VALUE_LIMIT)}")
    return clip(", ".join(parts), ARG_TOTAL_LIMIT)


class ToolExecutor:
    """执行 function calling 调用。ctx 提供回复目标、会话上下文与发送者权限。"""

    def __init__(self, cfg, storage, memory, bot):
        self.rt = ToolRuntime(cfg, storage, memory, bot)
        self.outbound = Outbound(cfg, storage, bot)
        self.registry = ToolRegistry(self.rt)
        counts: dict[str, int] = {}
        for t in self.registry.tools.values():
            counts[t.category] = counts.get(t.category, 0) + 1
        summary = " / ".join(f"{k} {v}" for k, v in sorted(counts.items()))
        logger.info("工具就绪: %d 个（%s）", len(self.registry.tools), summary or "无")

    async def send_text(self, ctx: dict, content) -> dict:
        """正文通道：不是工具调用，模型的话由这里发出去。"""
        sid = session_label(ctx.get("session_id"))
        try:
            result = await self.outbound.send(ctx, content)
        except Exception as e:  # noqa: BLE001
            logger.exception("发送正文异常", extra={"session": sid})
            ctx["tool_error"] = f"send_text: {e}"
            return {"ok": False, "error": str(e)}
        if isinstance(result, dict) and not result.get("ok", True):
            logger.warning("发送未成功: %s", clip(str(result.get("error") or result), 120),
                           extra={"session": sid})
        return result

    def schemas(self) -> list[dict]:
        return self.registry.schemas()

    def list_tools(self) -> list[dict]:
        return self.registry.list_info()

    def toggle(self, name: str, enabled: bool) -> dict | None:
        return self.registry.toggle(name, enabled)

    def set_level(self, name: str, level: int) -> dict | None:
        return self.registry.set_level(name, level)

    async def execute(self, name: str, args: dict, ctx: dict) -> dict:
        tool = self.registry.tools.get(name)
        if tool is None:
            logger.warning("工具 %s 不存在（模型编造的工具名）", name,
                           extra={"session": session_label(ctx.get("session_id"))})
            return {"ok": False, "error": f"未知工具 {name}"}
        runtime.tool()
        sid = session_label(ctx.get("session_id"))
        if not tool.is_enabled():
            logger.warning("工具 %s 未启用，调用被忽略", name, extra={"session": sid})
            return {"ok": False, "error": f"工具 {name} 未启用"}
        actor = ctx.get("actor") or {}
        level = int(actor.get("level") or 0)
        need = tool.required_level()
        if need > 5 or level < need:
            need_label = "永久锁" if need > 5 else f"L{need}"
            logger.warning("工具 %s 拒绝: 需要 %s，当前 %s(L%d)", name, need_label,
                           _actor_label(actor), level, extra={"session": sid})
            return {"ok": False, "error": f"权限不足：{name} 需要 {need_label}，当前 L{level}"}
        brief = _args_brief(args)
        call = f"工具 {name}（admin={_actor_label(actor)}(L{level})"
        call += f", {brief}）" if brief else "）"
        # 特权工具（L3+）在 INFO 留痕，普通工具只在 DEBUG 展开参数
        (logger.info if need >= 3 else logger.debug)(call, extra={"session": sid})
        try:
            result = await tool.run(args, ctx)
        except Exception as e:  # noqa: BLE001
            logger.exception("工具 %s 执行异常", name, extra={"session": sid})
            ctx["tool_error"] = f"{name}: {e}"
            return {"ok": False, "error": str(e)}
        if isinstance(result, dict) and not result.get("ok", True):
            logger.warning("工具 %s 未成功: %s", name, clip(str(result.get("error") or result), 120),
                           extra={"session": sid})
        return result


async def agent_loop(llm, executor, messages: list, ctx: dict, max_rounds: int = 6,
                     base_len: int | None = None) -> str:
    """function calling 循环：LLM 决策 → 执行工具 → 结果回传 → 继续，直到结束回合。

    工具列表是全部启用工具（对所有会话一致），权限只在实际调用时校验，不够就是失败返回。
    base_len 由调用方给出：messages 里属于「稳定前缀 + 历史」的条数，其余算本轮新增。

    模型唯一的说话通道是回复正文（正文经 outbound 通道发出，不在工具列表里，避免"调一次工具又说一遍"）：
    回合结束条件（返回 NO_REPLY，调用方不会把文本当回复发送）：
    - LLM 未调用工具：有文本一律按正文发出（本轮调用过工具也照发，
      工具往返之后的补充文本同样是要说的话），无文本直接结束；
    - LLM 同时给了工具和正文：先执行工具，再把正文发出去（"发张表情再补一句"是同一个动作）；
    - 某次工具调用是 silence → 立即结束，不再追加请求（沉默不该紧接着一句话）；
    - 达到 max_rounds。
    """
    sid = session_label(ctx.get("session_id"))
    tools = executor.schemas()
    if not tools:
        logger.warning("没有启用的工具，跳过本轮", extra={"session": sid})
        return NO_REPLY
    history = list(messages)
    base_len = (len(history) - 1) if base_len is None else max(0, min(int(base_len), len(history)))
    retried_placeholder = False
    for round_no in range(max_rounds):
        resp = await asyncio.to_thread(
            llm.chat, history, tools, base_len=base_len, session=sid
        )
        msg = resp.choices[0].message
        calls = msg.tool_calls or []
        if not calls:
            content = (msg.content or "").strip()
            if content and PLACEHOLDER_RE.match(content):
                logger.warning("模型用占位符代替工具调用: %s", content[:40], extra={"session": sid})
                if not retried_placeholder and round_no + 1 < max_rounds:
                    retried_placeholder = True
                    history.append({"role": "assistant", "content": content})
                    history.append({"role": "system", "content": PLACEHOLDER_HINT})
                    continue
                return NO_REPLY
            # 模型直接回文本一律照发：工具往返之后的补充文本同样是要说的话
            # （不另打一行说明——发出去的「发送: 」行本身就是记录）
            if content:
                await executor.send_text(ctx, content)
            else:
                logger.debug("模型未调用工具，结束本轮", extra={"session": sid})
            return NO_REPLY
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
            if ctx.get("silence"):
                # 选了沉默就到此为止：再问一次只会换来一句收尾话，正是要避免的
                return NO_REPLY
        # 工具和正文同轮出现时，正文同样是要说的话：媒体先落地，这句话跟在后面
        companion = (msg.content or "").strip()
        if companion:
            await executor.send_text(ctx, companion)
    # 工具循环超限：兜底结束（不回复）
    return NO_REPLY
