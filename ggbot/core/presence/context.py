"""上下文组装：一次请求 = 全量工具列表 + 静态人设 + 历史窗口 + 相关记忆 + 本轮消息。

前两段所有会话逐字节相同，是厂商上下文缓存的前缀；后三段每轮都换，所以一律排在后面。
这里只负责"拼出发给模型的 messages"，不决定发不发消息、也不动数据库里的状态。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ggbot.core.storage import REPLY_BACKLOG
from ggbot.util.media import media_kind_label
from ggbot.util.session import oid8, session_label

from ggbot.core.presence.defaults import MEMORY_BLOCK, SYSTEM_TEMPLATE

logger = logging.getLogger("ggbot.presence")

# 一轮里检索几条相关记忆：再多就喧宾夺主，模型该看的是眼下这段对话
MEMORY_HITS = 3
MEMORY_MIN_SCORE = 0.35
MEMORY_TIMEOUT_SEC = 5.0


def sender_label(m: dict) -> str:
    """发送者展示名 + 稳定的 8 位 id 尾巴：`小明#54019C24`。

    昵称是别人随时能改、也能撞车的字符串，只靠它会把两个人认成一个人、把同一个人认成两个人；
    openid 的前 8 位不变，所以同一个人前后几轮始终是同一个标签。没有昵称时只留 id，不写成 `id#id`。
    """
    name = (m.get("sender_name") or "").strip()
    tag = oid8(m.get("sender"))
    if not name:
        return tag or "unknown"
    return f"{name}#{tag}" if tag else name


def sender_label_plain(m: dict) -> str:
    """归档提炼用的裸昵称（不带 #id）：那一侧要的是「这条是关于谁的」，标签会污染 subject。"""
    return (m.get("sender_name") or "").strip() or oid8(m.get("sender")) or "unknown"


def fmt_user_messages(msgs: list[dict]) -> str:
    """把一条或多条入站消息格式化为模型输入（优先昵称，缺失用 8 位 id）。"""
    lines = []
    for m in msgs:
        parts = [f"[{sender_label(m)}]"]
        if m.get("is_at"):
            parts.append("(艾特了你)")
        content = m.get("content") or ""
        attachments = m.get("attachments", []) or []
        if content:
            parts.append(content)
        elif not attachments:
            parts.append("（无文本）")
        for a in attachments:
            ct = (a.get("content_type") or "").lower()
            url = a.get("url") or ""
            # 已落地的本地副本可作为 save_emoji / generate_image 的 source 转发
            local = a.get("local_path") or ""
            ref = f" url={url}" if url else ""
            if local:
                ref += f" local={local}"
            if not ref:
                continue  # 正文里已有 [图片 文件名] 标记，无需重复
            parts.append(f"[{media_kind_label(ct)}{ref}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def fmt_archive(msgs: list[dict]) -> str:
    """归档提炼用的纯文本记录：用户消息带裸昵称（不带 #id，免得把 subject 写成标签），助手消息标为「我」。"""
    lines = []
    for m in msgs:
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if m.get("role") == "assistant":
            lines.append(f"[我] {content}")
        else:
            lines.append(f"[{sender_label_plain(m)}] {content}")
    return "\n".join(lines)


@dataclass
class Assembled:
    """拼好的一次请求。"""

    messages: list[dict]
    base_len: int  # 属于「静态前缀 + 历史」的条数，其余算本轮新增（LLM 侧按它算缓存增量）
    window: list[dict]
    digest: str
    overflow_before_id: int | None = None  # 窗口满：这一段最旧的历史该交给归档了


class ContextBuilder:
    def __init__(self, cfg, storage, llm, memory=None):
        self.cfg = cfg
        self.storage = storage
        self.llm = llm
        self.memory = memory

    # ---------- 配置现读 ----------

    @property
    def system_prompt(self) -> str:
        """人设整段：现读 `llm.system_prompt`，留空或全空白就用代码里的内置默认。"""
        return (self.cfg.llm.get("system_prompt") or "").strip() or SYSTEM_TEMPLATE

    @property
    def history_limit(self) -> int:
        return int(self.cfg.context.get("max_history_messages", 1024))

    @property
    def budget_tokens(self) -> int:
        """上下文 tokens 预算，只做校验口径记日志；窗口按条数截断，不再按它压缩历史。"""
        return int(self.cfg.context.get("token_budget", 128000))

    # ---------- 各段 ----------

    def history_window(self, sid: str, turn_count: int, overflow: int) -> tuple[list[dict], int | None]:
        """归档游标之后的最近 history_limit 条，并给出满窗时要消化的位置。

        多取一些候选是为了剔除本轮还在等的消息后，窗口仍然装得满；
        本轮的 pending（reply_state=waiting）不进历史——它就在今次的「本轮消息」里。
        """
        fetch_n = self.history_limit + max(10, turn_count)
        history = self.storage.get_recent_messages(sid, fetch_n)
        cursor = self.storage.get_archive_cursor(sid)
        rows = [h for h in history
                if (h.get("reply_state") or "") != REPLY_BACKLOG and h["id"] > cursor]
        window = rows[-self.history_limit:] if self.history_limit > 0 else []
        keep_from = len(window) - overflow
        before_id = window[keep_from]["id"] if (len(window) >= self.history_limit and keep_from > 0) else None
        return window, before_id

    async def memory_block(self, sid: str, ctx_msg: dict) -> str:
        """相关记忆：按本轮内容检索全局 + 本会话，最多 3 条；没有命中就不注入这一段。

        命中即强化（touch）：被捞进上下文算"想起来过"一次，遗忘扫描按最后命中时间算淡出。
        """
        query = (ctx_msg.get("content") or "").strip()
        if not query or self.memory is None:
            return ""
        try:
            hits = await asyncio.wait_for(
                asyncio.to_thread(self.memory.search, query, MEMORY_HITS, sid,
                                  MEMORY_MIN_SCORE, True),
                timeout=MEMORY_TIMEOUT_SEC,
            )
        except Exception as e:  # noqa: BLE001
            logger.debug("记忆检索失败/超时，跳过注入: %s: %s", type(e).__name__, e,
                         extra={"session": session_label(sid)})
            return ""
        lines = [f"- {(h.get('content') or '')[:80]}" for h in hits]
        if not lines:
            return ""
        return MEMORY_BLOCK + "\n".join(lines)

    # ---------- 组装 ----------

    async def assemble(self, sid: str, msgs: list[dict], *, overflow: int) -> Assembled:
        """请求顺序就是缓存顺序：人设在前、历史次之，相关记忆与本轮消息放最后。

        每轮只换尾巴，前缀才命中得了；窗口装满时把要消化的最旧一段一并报给调用方
        （这里不碰归档，只负责"该消化了"这个判断）。
        """
        ctx_msg = msgs[-1] if msgs else {}
        digest = await self.memory_block(sid, ctx_msg)
        window, before_id = self.history_window(sid, len(msgs), overflow)
        messages = [{"role": "system", "content": self.system_prompt}]
        for h in window:
            if h["role"] == "user":
                messages.append({"role": "user", "content": f"[{sender_label(h)}] {h['content']}"})
            else:
                messages.append({"role": "assistant", "content": h["content"]})
        if digest:
            messages.append({"role": "system", "content": digest})
        messages.append({"role": "user",
                         "content": "【本轮消息】（只回应这里的内容）\n" + fmt_user_messages(msgs)})
        # 「历史」是本轮注入的历史消息条数，「估算」是整次请求的 tokens（口径见 LLM 请求行）。
        # 128k 预算装 1024 条历史足够，这里只留一行做观测，不再按它压缩历史。
        total = self.llm.estimate_messages_tokens(messages)
        logger.debug("上下文预算: 历史 %d/%d 条 · 估算 %d/%d tokens",
                     len(window), self.history_limit, total, self.budget_tokens,
                     extra={"session": session_label(sid)})
        return Assembled(messages=messages, base_len=1 + len(window), window=window,
                         digest=digest, overflow_before_id=before_id)
