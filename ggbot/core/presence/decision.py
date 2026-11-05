"""发言决策：一批消息摆在这儿，决定说不说、怎么说、说完记什么账。

三条路径共用同一条出站（outbox）：
- process_batch：在线窗口聚合好的一批；
- reply_now：被@唤醒，单条直接回；
- speak：回合外发言（提醒到点、管理代发），没有本轮消息也要能说。

三条都是 outbox.open_turn（有锚点走被动回复，没锚点才问过主动频控闸门）→
上下文组装 + agent 循环（模型自己决定说还是调 silence）→ 把结果记回 messages 的
reply_state → outbox.commit_turn 落锚点序号与频控计数。
作息的记账挂在出站 ctx 上，当场发生：真发出去一条就续一次期（本轮累计封顶），
向模型发一次请求只保命不续期，见 _open_turn。
"""

from __future__ import annotations

import logging

from tool import agent_loop
from ggbot.core.outbox import Outbox
from ggbot.core.storage import REPLY_ANSWERED, REPLY_FAILED, REPLY_SKIPPED
from ggbot.util.logging_setup import clip
from ggbot.util.session import session_label

from ggbot.core.presence.state import SessionState

logger = logging.getLogger("ggbot.presence")


class TurnDecider:
    def __init__(self, storage, llm, perms, context, archiver, executor, outbox: Outbox):
        self.storage = storage
        self.llm = llm
        self.perms = perms
        self.context = context  # 上下文组装
        self.archiver = archiver  # 满窗时交给它的归档
        self.executor = executor  # 工具执行器：agent 循环与正文通道都在这里
        self.outbox = outbox

    # ---------- 三条发言路径 ----------

    async def process_batch(self, msgs: list[dict], st: SessionState | None = None) -> bool:
        """在线批次：聚合成一轮决策，先试被动回复，回不去才看主动闸门。

        普通群消息（全量事件）的 msg_id 同样能被动回复，这样不占主动消息额度；
        主动发送受平台 20 条/分钟、1000 条/天（群主需开启「机器人主动在群聊内发言」）
        与本地 proactive_per_minute/proactive_per_day 约束，被闸门拦下则本轮不回复。
        """
        sid = msgs[-1].get("session_id") or ""
        ctx, blocked = self._open_turn(sid, msgs, st)
        if ctx is None:
            label = st.label if st else session_label(sid)
            logger.debug("在线普通消息批次跳过回复（%s）%d 条", blocked, len(msgs),
                         extra={"session": label})
            return False
        return await self._run_turn(msgs, ctx)

    async def reply_now(self, inbound: dict, st: SessionState | None = None) -> bool:
        """唤醒即时回复：单条消息直接走一个回合。"""
        sid = inbound.get("session_id") or ""
        ctx, _blocked = self._open_turn(sid, [inbound], st)
        if ctx is None:
            return False
        return await self._run_turn([inbound], ctx)

    async def speak(self, sid: str, text: str, st: SessionState | None = None) -> bool:
        """不经模型往会话里说一句话（提醒到点就走这条通道），发送路径和回合内一致。

        先找还能用的锚点走被动回复，没有才主动发送——主动被本地频控闸门拦下就返回 False，
        由调用方下一轮重试。发出去那一下的续期由 ctx 上的钩子记（和回合内同一个口子），
        这里不再补记一次。
        """
        ctx, blocked = self._open_turn(sid, [], st)
        if ctx is None:
            logger.debug("主动发言被拦（%s）: %s", blocked, clip(text, 40),
                         extra={"session": session_label(sid)})
            return False
        label = session_label(sid)
        try:
            result = await self.executor.outbound.send(ctx, text)
        except Exception as e:  # noqa: BLE001
            logger.warning("发言失败: %s", clip(str(e), 80), extra={"session": label})
            return False
        if not ctx.get("sent_any"):
            logger.warning("发言未送出: %s", clip(str(result.get("error") or result), 80),
                           extra={"session": label})
            return False
        self.outbox.commit_turn(sid, ctx)
        return True

    # ---------- 出站与回合 ----------

    def _open_turn(self, sid: str, msgs: list[dict],
                   st: SessionState | None = None) -> tuple[dict | None, str | None]:
        target, scene = self._target_scene(sid, msgs)
        ctx, blocked = self.outbox.open_turn(sid, target=target, scene=scene, msgs=msgs)
        if ctx is not None and st is not None:
            # 作息的两个记账点挂在 ctx 上，发送与请求那两侧各自只叫一声：
            # on_sent = 真发出去一条（正式续期，本轮累计封顶），
            # on_request = 又要问一次模型（只保命，免得一轮跑到一半被判离线）
            st.begin_round()
            ctx["on_sent"] = st.renew
            ctx["on_request"] = st.keep_alive
        return ctx, blocked

    def _target_scene(self, sid: str, msgs: list[dict]) -> tuple[str, str]:
        """这一轮往哪儿发：本轮消息自带就用它，只有会话（回合外发言）就从库里反查。"""
        m = msgs[-1] if msgs else {}
        target, scene = self.outbox.target_for(sid)
        return m.get("target") or target, m.get("scene") or scene

    async def _run_turn(self, msgs: list[dict], ctx: dict) -> bool:
        """跑一个回合并把结果记账。任何失败只落状态，不向上抛（免得单条失效群打崩整轮积压）。

        结果只看两件事：回合里有没有真的发出去（ctx.sent_any，正文或媒体都算），
        以及有没有工具报错。没发出去也没报错 = 模型选择沉默/没话可说。
        续期不在这里补记——发出去那一下由 ctx 上的钩子当场记。
        """
        sid = ctx["session_id"]
        label = session_label(sid)
        ids = [m["msg_id"] for m in msgs if m.get("msg_id")]
        try:
            await self._agent_loop(sid, msgs, ctx)
        except Exception as e:  # noqa: BLE001
            logger.warning("回复生成失败 n=%d: %s", len(msgs), e, extra={"session": label})
            self.storage.finish_backlog(ids, REPLY_FAILED)
            return False
        if ctx.get("sent_any"):
            self.storage.finish_backlog(ids, REPLY_ANSWERED)
            self.outbox.commit_turn(sid, ctx)
            return True
        if ctx.get("tool_error"):
            if ctx.get("passive"):
                logger.warning("工具执行失败 n=%d: %s", len(msgs), ctx["tool_error"],
                               extra={"session": label})
            else:
                logger.warning("主动回复失败 n=%d（群主需开启「机器人主动在群聊内发言」）: %s",
                               len(msgs), ctx["tool_error"], extra={"session": label})
            self.storage.finish_backlog(ids, REPLY_FAILED)
            return False
        self.storage.finish_backlog(ids, REPLY_SKIPPED)
        return False

    async def _agent_loop(self, sid: str, msgs: list[dict], ctx: dict) -> str:
        """组装上下文并交给 agent 循环：模型写正文、调工具，实际发送由运行时完成。"""
        ctx_msg = msgs[-1] if msgs else {}
        # 权限主体 = 本轮触发者（批次最后一条），由服务端解析，不接受模型参数
        actor = self.perms.resolve(ctx_msg.get("sender"), ctx_msg.get("sender_name"))
        ctx["actor"] = actor
        if actor["level"] <= 0:
            return ""
        assembled = await self.context.assemble(sid, msgs, overflow=self.archiver.overflow)
        if assembled.overflow_before_id is not None:
            # 窗口已满：把最旧一段交给后台分批写入记忆，不阻塞本轮回复
            self.archiver.schedule(sid, assembled.overflow_before_id)
        return await agent_loop(self.llm, self.executor, assembled.messages, ctx,
                                base_len=assembled.base_len)
