"""在线作息：单会话的在线/离线倒计时与续期。

这一层只有时间，没有消息：什么时候上线、这一轮能在线多久、回了一句话之后延到什么时候。
读积压、聚合批次、发回复都不在这里，由入站编排拿这里的结论去驱动。
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable

from ggbot.util.human import jitter, sample_duration


class SessionState:
    """一个会话的作息。

    离线：倒计时到点上线；首次上线在离线周期内随机错开，避免所有会话整点同时上线。
    在线：倒计时结束下线；每成功回复一轮固定续期（默认 30s，封顶 renew_max_sec）。
    """

    def __init__(self, sid: str, label: str, settings: Callable[[], dict]) -> None:
        self.sid = sid
        self.label = label
        self._settings = settings  # 现读 cfg.presence：面板改完下一轮就生效，不重启进程
        self.state = "offline"
        self.reason = "initial"
        self.activity = 0  # 本在线周期收到的消息数：决定上下线日志等级与下线小结
        self.last_active = time.monotonic()
        self.online_deadline = 0.0
        self.next_online = self.last_active + self.offline_span() * random.uniform(0.4, 1.0)

    @property
    def p(self) -> dict:
        return self._settings()

    @property
    def is_online(self) -> bool:
        return self.state == "online"

    def online_span(self) -> float:
        """本次在线时长：基准 ±online_duration_jitter。"""
        return sample_duration(
            float(self.p.get("online_duration_sec", 60)),
            float(self.p.get("online_duration_jitter", 0.3)),
            minimum=5.0,
        )

    def offline_span(self) -> float:
        """本次离线时长：基准 ±offline_interval_jitter。"""
        return sample_duration(
            float(self.p.get("offline_interval_sec", 600)),
            float(self.p.get("offline_interval_jitter", 0.4)),
            minimum=10.0,
        )

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def go_online(self, reason: str) -> float | None:
        """切到在线，返回本次在线时长（秒）；本来就在线则返回 None，什么都不改。"""
        if self.is_online:
            return None
        self.state = "online"
        self.reason = reason
        span = self.online_span()
        self.online_deadline = time.monotonic() + span
        self.touch()
        self.activity = 0
        return span

    def go_offline(self, reason: str) -> float | None:
        """切到离线，返回本次离线时长（秒）；本来就离线则返回 None。

        抖动窗口里还没发出去的批次由入站编排负责清掉，这里只挪时间。
        """
        if not self.is_online:
            return None
        self.state = "offline"
        self.reason = reason
        wait = self.offline_span()
        self.next_online = time.monotonic() + wait
        return wait

    def renew(self) -> None:
        """每成功回复一轮：在线时长固定续期（默认 30s，封顶 renew_max_sec）。"""
        if not self.p.get("renew_on_reply", True):
            return
        inc = jitter(float(self.p.get("renew_per_reply_sec", 30)),
                     float(self.p.get("renew_jitter", 0.3)), minimum=0.0)
        cap = max(0.0, float(self.p.get("renew_max_sec", 600)))
        if cap:
            inc = min(inc, cap)
        self.online_deadline = max(self.online_deadline, time.monotonic()) + inc
        self.reason = "reply-renew"
        self.touch()

    def hurry_online(self, within_sec: float) -> None:
        """把上线倒计时压到 within_sec 之内（替它主动说完话，接下来得尽快在场）。"""
        self.next_online = min(self.next_online, time.monotonic() + within_sec)

    def expired(self, now: float) -> bool:
        """当前状态的倒计时是否已走完（tick 用它决定该上线还是该下线）。"""
        return (self.online_deadline if self.is_online else self.next_online) <= now

    def countdown(self, now: float) -> tuple[str, float]:
        """面板展示用的下一个事件与剩余秒数。"""
        if self.is_online:
            return "离线倒计时", max(0.0, self.online_deadline - now)
        return "上线倒计时", max(0.0, self.next_online - now)
