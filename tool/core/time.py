"""当前时间：让模型在需要日期/星期时取真实时间，而不是凭记忆猜。"""

from __future__ import annotations

from datetime import datetime

from ..base import Tool

_WEEKDAYS = "一二三四五六日"


class CurrentTime(Tool):
    name = "time"
    description = (
        "获取当前日期、时间、星期与时区。回答“今天几号”“现在几点”“星期几”，"
        "或写带日期的文案时调用，不要凭记忆猜"
    )
    parameters = {"type": "object", "properties": {}}
    prompt_hint = "涉及今天几号、现在几点、星期几，或要写带时间的文案时，先调 time 取当前时间。"

    async def run(self, args: dict, ctx: dict) -> dict:
        now = datetime.now().astimezone()
        return {
            "ok": True,
            "datetime": now.isoformat(timespec="seconds"),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": f"星期{_WEEKDAYS[now.weekday()]}",
            "timezone": now.tzname() or "",
        }


def get_tools(rt):
    return [CurrentTime(rt)]
