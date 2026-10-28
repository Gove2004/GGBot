"""拟人化时序：把机械的固定秒数换成带抖动的区间。

在线/离线时长与续期时长每次都不完全一样，避免整点打卡式作息。
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
