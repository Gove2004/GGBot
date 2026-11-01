"""Presence 包：机器人「什么时候在场、说什么、怎么说」。

拆成四段，各管一件事（装配见 manager.py）：
- state.py —— 在线作息：一个会话什么时候上线、这一轮在线多久、回了一句话之后延到什么时候；
- inbound.py —— 入站编排：权限、入库、上下线、离线积压只读、在线窗口聚合；
- context.py —— 上下文组装：静态人设 + 历史窗口 + 相关记忆 + 本轮消息；
- decision.py —— 发言决策：这一轮说不说、走被动回复还是主动发送、说完记什么账。

历史归档（archiver.py）挂在满窗之后，默认人设（defaults.py）是给面板「人设」页兜底的原文。

作息是会话级的：私聊唤醒不会让群聊上线，A 群唤醒不影响 B 群；离线周期到点自然上线，
被@则立刻醒并把旧积压只读不回。进程重启后该会话的第一条消息直接上线接话
（start_online，默认开），否则每条新会话都要先压满一个离线周期才像「活过来」。
"""

from ggbot.core.presence.defaults import MEMORY_BLOCK, SYSTEM_TEMPLATE
from ggbot.core.presence.manager import PresenceManager

__all__ = ["PresenceManager", "SYSTEM_TEMPLATE", "MEMORY_BLOCK"]
