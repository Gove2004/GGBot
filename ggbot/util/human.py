"""拟人化时序：把机械的固定秒数换成带抖动的区间。

- 在线/离线时长抖动：每次上下线都不完全一样，避免整点打卡式作息
- 回复前阅读延迟：看到消息后先停顿再开始处理
- 输入延迟：按回复字数估算“打字”耗时，避免瞬间秒回
"""

from __future__ import annotations

import random


def jitter(value: float, ratio: float = 0.25, minimum: float = 0.0) -> float:
    """按比例上下浮动（ratio=0.2 表示 ±20%），不低于 minimum。"""
    if ratio <= 0:
        return max(minimum, value)
    return max(minimum, value * random.uniform(1.0 - ratio, 1.0 + ratio))


def sample_duration(base: float, ratio: float = 0.3, minimum: float = 1.0) -> float:
    """采样一个带抖动的时长（秒）。"""
    return jitter(base, ratio, minimum)


def read_delay_sec(cfg: dict) -> float:
    """读到消息后到开始处理之间的停顿（秒）；未配置时不做延迟。"""
    lo = max(0.0, float((cfg or {}).get("read_delay_min_sec", 0.0) or 0))
    hi = max(0.0, float((cfg or {}).get("read_delay_max_sec", 0.0) or 0))
    if hi <= 0:
        return 0.0
    if hi < lo:
        lo, hi = hi, lo
    return random.uniform(lo, hi)


def typing_delay_sec(text: str, cfg: dict) -> float:
    """按内容长度估算“打字”耗时（秒），带抖动并限制上下限；未配置时不做延迟。"""
    cps = float((cfg or {}).get("typing_chars_per_sec", 0) or 0)
    n = len(text or "")
    if cps <= 0 or n == 0:
        return 0.0
    cfg = cfg or {}
    lo = max(0.0, float(cfg.get("typing_min_sec", 0.4) or 0))
    hi = max(lo, float(cfg.get("typing_max_sec", 4.0) or 0))
    spread = max(0.0, float(cfg.get("typing_jitter", 0.25) or 0))
    return max(lo, min(hi, jitter(n / cps, spread)))
