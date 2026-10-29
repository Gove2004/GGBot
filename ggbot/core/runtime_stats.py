"""进程内运行统计：本次启动以来的会话、消息、工具与 LLM 用量。

只做内存累加（重启归零），供 Web 面板与日志展示；不落库，避免和持久化统计混淆。
"""

from __future__ import annotations

import threading
import time


class RuntimeStats:
    def __init__(self) -> None:
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._sessions: set[str] = set()
        self.received = 0
        self.sent = 0
        self.tool_calls = 0
        self.llm_calls = 0
        self.vision_calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.cost = 0.0
        self.priced = False
        self.currency = "¥"

    def inbound(self, session_id: str | None) -> None:
        with self._lock:
            self.received += 1
            if session_id:
                self._sessions.add(session_id)

    def outbound(self) -> None:
        with self._lock:
            self.sent += 1

    def tool(self) -> None:
        with self._lock:
            self.tool_calls += 1

    def llm(self, prompt: int | None, completion: int | None,
            cached: int | None, cost: float | None, currency: str,
            vision: bool = False) -> None:
        with self._lock:
            self.llm_calls += 1
            if vision:
                self.vision_calls += 1
            self.prompt_tokens += max(0, int(prompt or 0))
            self.completion_tokens += max(0, int(completion or 0))
            self.cached_tokens += max(0, int(cached or 0))
            if cost is not None:
                self.cost += float(cost)
                self.priced = True
            if currency:
                self.currency = currency

    def snapshot(self) -> dict:
        with self._lock:
            tokens = self.prompt_tokens + self.completion_tokens
            cost_text = "-"
            if self.priced:
                cost_text = f"{self.currency}{self.cost:.6f}".rstrip("0").rstrip(".")
            return {
                "uptime_sec": int(time.time() - self.started_at),
                "sessions": len(self._sessions),
                "received": self.received,
                "sent": self.sent,
                "tool_calls": self.tool_calls,
                "llm_calls": self.llm_calls,
                "vision_calls": self.vision_calls,
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "cached_tokens": self.cached_tokens,
                "tokens": tokens,
                "cost": round(self.cost, 6),
                "cost_text": cost_text,
                "currency": self.currency,
            }


runtime = RuntimeStats()
