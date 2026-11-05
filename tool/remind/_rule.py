"""日程的时间写法与周期规则：整个项目只有这一份解析。

对话侧的 `when` / `repeat` 走这里（模型只填固定写法，自然语言先调 `time` 换算）；
面板不给解析——它直接写绝对 unix 秒（日期时间选择器算出来的），所以第二套"猜时间"的
实现没有存在的必要。

- `parse_when`：把 `+30m` / `18:30` / `10-30 18:30` / `2026-10-30 18:30` 换成到期 unix 秒
- `parse_rule`：把 `daily` / `weekdays` / `weekly` / `monthly` 归一成一个自描述的规则串，
  月周期把"几号"钉在串里（`monthly:31`），否则 1 月 31 日会一路漂成 28 号
- `advance`：给一个到期时间和规则，算下一场的到期时间（从**原本的那一格**往后推，
  不从"现在"推，每天 8 点才不会因为晚发两分钟漂成 8:02）

时区口径：全都按**服务器本地时间**理解和计算（`time` 工具返回的就是它），存的是 unix 秒。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_WEEKDAYS = "一二三四五六日"
_UNITS = {"m": 60, "h": 3600, "d": 86400}
_REL_RE = re.compile(r"^\+(\d{1,5})([mhd])$")
_HM_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")
_MD_RE = re.compile(r"^(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")
_YMD_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$")

WHEN_HELP = (
    "when 只认这几种写法：+30m / +2h / +1d（从现在起多久）、18:30（今天或明天的这个点）、"
    "10-30 18:30（今年）、2026-10-30 18:30"
)
REPEAT_HELP = (
    "repeat 只认这几种：daily（每天）/ weekdays（工作日，跳过周六日）/ weekly（每周同一天）/ "
    "monthly（每月同一号，遇到没这一天就当月最后一天）；不填就是一次性提醒"
)

_RULE_WORDS = {
    "": "", "none": "", "once": "", "single": "", "一次性": "", "单次": "",
    "daily": "daily", "day": "daily", "everyday": "daily", "每天": "daily", "每日": "daily",
    "weekdays": "weekdays", "workdays": "weekdays", "weekday": "weekdays", "工作日": "weekdays",
    "weekly": "weekly", "week": "weekly", "everyweek": "weekly", "每周": "weekly", "每星期": "weekly",
    "monthly": "monthly", "month": "monthly", "everymonth": "monthly", "每月": "monthly",
}


def _build(year, month, day, hour, minute, second) -> datetime | None:
    """按当前时区拼一个时间点，越界（13 月、2 月 30 日）返回 None。"""
    try:
        return datetime.now().astimezone().replace(
            year=int(year), month=int(month), day=int(day),
            hour=int(hour), minute=int(minute), second=int(second or 0), microsecond=0,
        )
    except ValueError:
        return None


def parse_when(raw) -> tuple[int, str]:
    """把 when 换成 unix 秒；换不出来返回 (0, 错误说明)。只认固定写法，自然语言由模型换算。"""
    now = datetime.now().astimezone()
    s = str(raw or "").strip().replace("：", ":")
    if not s:
        return 0, f"when 不能为空。{WHEN_HELP}"

    m = _REL_RE.match(s)
    if m:
        return int(now.timestamp()) + int(m.group(1)) * _UNITS[m.group(2)], ""

    m = _HM_RE.match(s)
    if m:
        try:
            due = now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                               second=int(m.group(3) or 0), microsecond=0)
        except ValueError:
            return 0, f"时间不合法：{s}。{WHEN_HELP}"
        if due <= now:
            due += timedelta(days=1)  # 只说「18:30」而今天已经过了 → 明天这个点
        return int(due.timestamp()), ""

    m = _MD_RE.match(s)
    if m:
        due = _build(now.year, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        if due is None:
            return 0, f"日期不合法：{s}。{WHEN_HELP}"
        if due <= now:
            due = _build(now.year + 1, m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
            if due is None:
                return 0, f"日期不合法：{s}。{WHEN_HELP}"
        return int(due.timestamp()), ""

    m = _YMD_RE.match(s)
    if m:
        due = _build(int(m.group(1)), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6))
        if due is None:
            return 0, f"日期不合法：{s}。{WHEN_HELP}"
        return int(due.timestamp()), ""

    return 0, f"看不懂这个时间：{s}。{WHEN_HELP}"


def parse_rule(raw, due_ts: int) -> tuple[str, str]:
    """把 repeat 归一成规则串；月周期把「几号」钉进串里，防止月末一路漂下去。

    返回 (rule, 错误说明)。规则串自己就够 describe：`''` / `daily` / `weekdays` /
    `weekly` / `monthly:31`。
    """
    s = str(raw or "").strip().lower()
    word = _RULE_WORDS.get(s)
    if word is None:
        return "", f"repeat 看不懂：{raw}。{REPEAT_HELP}"
    if word == "monthly":
        return f"monthly:{datetime.fromtimestamp(int(due_ts)).day}", ""
    return word, ""


def rule_label(rule: str) -> str:
    """规则串说回人话，面板和工具返回都用它，不再各写一份映射。"""
    return {
        "": "一次性",
        "daily": "每天",
        "weekdays": "工作日",
        "weekly": "每周",
    }.get(rule) or (f"每月{rule.split(':', 1)[1]}号" if rule.startswith("monthly:") else rule or "一次性")


def _month_end(year: int, month: int) -> int:
    if month == 12:
        return 31
    return (datetime(year, month + 1, 1) - timedelta(days=1)).day


def advance(due_ts: int, rule: str) -> int:
    """一场结束后的下一场到期时间（unix 秒）。一次性规则返回 0，调用方按"跑完了"处理。"""
    dt = datetime.fromtimestamp(int(due_ts))
    if rule in ("daily",):
        return int((dt + timedelta(days=1)).timestamp())
    if rule == "weekly":
        return int((dt + timedelta(days=7)).timestamp())
    if rule == "weekdays":
        nxt = dt + timedelta(days=1)
        while nxt.weekday() >= 5:  # 5=周六 6=周日
            nxt += timedelta(days=1)
        return int(nxt.timestamp())
    if rule.startswith("monthly:"):
        try:
            pin = max(1, min(31, int(rule.split(":", 1)[1])))
        except (ValueError, IndexError):
            pin = dt.day
        year, month = (dt.year, dt.month + 1) if dt.month < 12 else (dt.year + 1, 1)
        day = min(pin, _month_end(year, month))
        return int(dt.replace(year=year, month=month, day=day).timestamp())
    return 0


def fmt_due(ts) -> str:
    """绝对时间 + 星期，工具与 CLI 的显示口径。"""
    try:
        dt = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OSError):
        return "—"
    return f"{dt:%Y-%m-%d %H:%M} 星期{_WEEKDAYS[dt.weekday()]}"


def span_text(sec: int) -> str:
    """把一段时间说成人话：35分 / 2小时5分 / 3天（天以下的精度到分）。"""
    sec = max(0, int(sec))
    days, rem = divmod(sec, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes and not days:
        parts.append(f"{minutes}分")
    return "".join(parts) or "不到一分钟"
