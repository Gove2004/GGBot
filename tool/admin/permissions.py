"""权限管理：归属者调整成员等级（grant / revoke / list / help）。

- 只有 L5 能调用：执行器按门槛拦截，工具内部再校验一次，避免门槛被下调后越权；
- 身份只来自本轮消息发送者（ctx.actor），目标成员按昵称 / 8 位 id / 完整 openid 匹配；
- 不能授予高于自己的等级，也不能改动同级或更高等级的成员（自己除外，可用 Web 恢复）。
"""

from __future__ import annotations

import asyncio
import logging

from ggbot.core.auth import (DEFAULT_LEVEL, LEVEL_LOCKED, LEVEL_LOCKED_NAME, LEVEL_MAX,
                             LEVEL_NAMES, clamp_level)
from ggbot.util.session import session_label

from ..base import Tool

logger = logging.getLogger("ggbot.tool.admin")

# 正式名字表只有一份（core.auth.LEVEL_NAMES），这里补的是口语说法：管理员说的是「拉黑、主人」这类词
LEVEL_ALIASES = {
    "拉黑": 0, "封禁": 0, "禁用": 0, "黑名单": 0, "踢出": 0,
    "限制": 1, "只聊天": 1,
    "标准": 2, "正常": 2,
    "高级": 3,
    "管理": 4, "管理员": 4,
    "主人": 5, "群主": 5, "拥有者": 5,
}
LEVEL_ALIASES.update({name: level for level, name in LEVEL_NAMES.items()})

LEVEL_ABILITIES = {
    0: "消息直接丢弃，不回复、不入库",
    1: "只能聊天、发表情、查记忆",
    2: "加上联网搜索与抓网页、发语音、写记忆、简单存储（kv_*）、收藏表情",
    3: "加上发邮件",
    4: "加上生图、排定时任务（到点会真跑一轮模型，所以这一档起）",
    5: "拥有 4 的全部能力，并可调整他人权限、执行 shell",
}


LEVEL_HINT = f"0-{LEVEL_MAX} 或 {'/'.join(LEVEL_NAMES.values())}"
OWNER_LABEL = f"L{LEVEL_MAX} {LEVEL_NAMES[LEVEL_MAX]}"
# 发给模型的那份等级说明也从名字表与能力表现拼，改档位或改能力归属不用回来改文案
LEVEL_ROSTER = " / ".join(
    f"{lv} {LEVEL_NAMES[lv]}（{LEVEL_ABILITIES[lv]}）" for lv in range(LEVEL_MAX + 1))


def parse_level(value) -> int:
    if isinstance(value, bool):
        raise ValueError(f"等级必须是 {LEVEL_HINT}")
    if isinstance(value, int):
        level = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"缺少等级：{LEVEL_HINT}")
        if text.isdigit():
            level = int(text)
        elif text in LEVEL_ALIASES:
            level = LEVEL_ALIASES[text]
        else:
            raise ValueError(f"无法识别的等级: {text}（可用 {LEVEL_HINT}）")
    if not 0 <= level <= LEVEL_MAX:
        raise ValueError(
            f"等级范围是 0-{LEVEL_MAX}（{LEVEL_LOCKED} 为{LEVEL_LOCKED_NAME}，只能在 Web 工具页设置）")
    return level


def _oid8(openid: str) -> str:
    return (openid or "")[:8].upper()


class AdminManage(Tool):
    name = "admin_manage"
    min_level = LEVEL_MAX
    data_name = "admin"
    description = (
        f"调整成员在本机的权限等级（只有 {OWNER_LABEL} 可用）。"
        f"action=grant 授予/升降级，action=revoke 收回（设为 0 {LEVEL_NAMES[0]}），"
        "action=list 查看成员与等级，action=help 查看等级含义和说法示例。"
        "target 可以填昵称、8 位 id 或完整 openid；昵称重名时会返回候选，不要猜。"
        f"等级：{LEVEL_ROSTER}。"
        "对方只描述能力时按这份对照换算成级别；说不清要哪一级就先问一句，或调 help "
        "把档位列全让对方选，对方问“权限怎么分”就照 help 的内容回答。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["grant", "revoke", "list", "help"],
                "description": "grant 授予/调整，revoke 收回，list 查看，help 说明",
            },
            "target": {
                "type": "string",
                "description": "成员昵称、8 位 id 或完整 openid（grant/revoke 必填）",
            },
            "level": {
                "type": "string",
                "description": f"grant 用：{LEVEL_HINT}",
            },
            "note": {"type": "string", "description": "可选备注"},
        },
        "required": ["action"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        actor = ctx.get("actor") or {}
        caller = int(actor.get("level") or 0)
        if caller < LEVEL_MAX:
            return {"ok": False, "error": f"权限管理仅限 {OWNER_LABEL}"}
        action = str(args.get("action") or "").strip().lower()
        if action == "help":
            return self._help()
        if action == "list":
            return self._list()
        if action not in {"grant", "revoke"}:
            return {"ok": False, "error": f"未知操作: {action}（grant/revoke/list/help）"}

        raw = str(args.get("target") or "").strip().lstrip("@")
        if not raw:
            return {"ok": False, "error": "缺少 target：请给昵称、8 位 id 或完整 openid"}
        matches = self._match(raw)
        if len(matches) > 1:
            return {
                "ok": False,
                "error": f"找到 {len(matches)} 个匹配成员，请用 8 位 id 或完整 openid 指定",
                "candidates": [self._brief(m) for m in matches[:8]],
            }
        if matches:
            target = matches[0]
        elif self._looks_like_openid(raw):
            target = {"openid": raw, "name": "", "level": None, "explicit": False}
        else:
            return {"ok": False, "error": "没找到这个成员；先用 action=list 查，或提供完整 openid"}

        openid = target["openid"]
        row = self.rt.storage.get_actor(openid)
        old = int(row["level"]) if row else None
        is_self = openid == (actor.get("openid") or "")
        if not is_self and old is not None and old >= caller:
            return {
                "ok": False,
                "error": f"{self._label(target, old)} 是 L{old}（同级或更高），需在 Web 权限页调整",
            }

        if action == "revoke":
            level = 0
        else:
            try:
                level = parse_level(args.get("level"))
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            if level > caller:
                return {
                    "ok": False,
                    "error": f"不能授予 L{level}，高于你当前的 L{caller}",
                }

        note = str(args.get("note") or "").strip() or None
        data = await asyncio.to_thread(
            self.rt.storage.set_actor,
            openid,
            level,
            target.get("name") or None,
            note,
        )
        logger.info(
            "权限调整: %s -> L%d (%s), 操作者 %s(L%d)",
            self._label(target, old), level, LEVEL_NAMES[level],
            actor.get("name") or _oid8(actor.get("openid")), caller,
            extra={"session": session_label(ctx.get("session_id"))},
        )
        result = {
            "ok": True,
            "openid": openid,
            "id8": _oid8(openid),
            "name": data.get("name") or target.get("name") or "",
            "previous_level": old,
            "level": level,
            "level_name": LEVEL_NAMES[level],
            "note": data.get("note") or "",
        }
        if is_self and level < LEVEL_MAX:
            result["warning"] = "你已降低自己的等级；需要恢复请在 Web 权限页修改"
        return result

    # ---------- 查询 ----------

    def _help(self) -> dict:
        return {
            "ok": True,
            "levels": [
                {"level": lv, "name": LEVEL_NAMES[lv], "ability": LEVEL_ABILITIES[lv]}
                for lv in range(LEVEL_MAX + 1)
            ],
            "examples": [
                f"“把小明设成{LEVEL_NAMES[3]}” → grant target=小明 level=3",
                "“把小明设成 3” → grant target=小明 level=3",
                "“取消小林的权限 / 拉黑他” → revoke target=小林",
            ],
            "note": (f"能力对应哪一级看 levels，别自己猜；成员重名时先用 list 看 8 位 id，再用 id 指定；"
                     f"{LEVEL_LOCKED} 为{LEVEL_LOCKED_NAME}，只能在 Web 工具页设置。"),
        }

    def _list(self) -> dict:
        default = self._default_level()
        members = []
        for m in self._members():
            level = m["level"] if m["level"] is not None else default
            members.append({
                **self._brief(m),
                "level": level,
                "level_name": LEVEL_NAMES.get(level, f"L{level}"),
                "explicit": m["explicit"],
                "messages": m.get("messages", 0),
            })
        members.sort(key=lambda r: (-r["level"], 0 if r["explicit"] else 1, -r["messages"]))
        return {
            "ok": True,
            "default_level": default,
            "count": len(members),
            "members": members[:50],
            "truncated": len(members) > 50,
        }

    def _default_level(self) -> int:
        """未登记成员的默认级别：与 Permissions 读的是同一条口径（clamp 后落 0~LEVEL_MAX）。"""
        data = getattr(self.rt.cfg, "data", None) or {}
        p = data.get("permissions") if isinstance(data, dict) else None
        return clamp_level((p or {}).get("default_level", DEFAULT_LEVEL))

    # ---------- 成员匹配 ----------

    def _members(self) -> list[dict]:
        rows: dict[str, dict] = {}
        for a in self.rt.storage.list_actors():
            openid = a.get("openid") or ""
            if not openid:
                continue
            rows[openid] = {
                "openid": openid,
                "name": a.get("name") or "",
                "level": int(a.get("level") or 0),
                "explicit": True,
                "messages": 0,
            }
        for s in self.rt.storage.list_senders(500):
            openid = s.get("sender") or ""
            if not openid:
                continue
            item = rows.get(openid)
            if item is None:
                item = rows[openid] = {
                    "openid": openid,
                    "name": s.get("sender_name") or "",
                    "level": None,
                    "explicit": False,
                    "messages": 0,
                }
            item["name"] = item["name"] or (s.get("sender_name") or "")
            item["messages"] = int(s.get("messages") or 0)
        return list(rows.values())

    def _match(self, raw: str) -> list[dict]:
        low = raw.lower()
        rows = self._members()
        for r in rows:
            if r["openid"].lower() == low:
                return [r]
        prefix = [r for r in rows if r["openid"][:8].lower() == low]
        if prefix:
            return prefix
        exact = [r for r in rows if (r["name"] or "").lower() == low]
        if exact:
            return exact
        return [r for r in rows if low and low in (r["name"] or "").lower()]

    @staticmethod
    def _looks_like_openid(raw: str) -> bool:
        return len(raw) >= 16 and " " not in raw

    @staticmethod
    def _brief(row: dict) -> dict:
        return {
            "openid": row["openid"],
            "id8": _oid8(row["openid"]),
            "name": row.get("name") or "",
        }

    @classmethod
    def _label(cls, row: dict, level: int | None) -> str:
        name = row.get("name") or ""
        tag = f"{name}/{_oid8(row['openid'])}" if name else _oid8(row["openid"])
        return f"{tag}(L{level})" if level is not None else tag


def get_tools(rt):
    return [AdminManage(rt)]
