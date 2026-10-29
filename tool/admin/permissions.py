"""权限管理：归属者调整成员等级（grant / revoke / list / help）。

- 只有 L5 能调用：执行器按门槛拦截，工具内部再校验一次，避免门槛被下调后越权；
- 身份只来自本轮消息发送者（ctx.actor），目标成员按昵称 / 8 位 id / 完整 openid 匹配；
- 不能授予高于自己的等级，也不能改动同级或更高等级的成员（自己除外，可用 Web 恢复）。
"""

from __future__ import annotations

import asyncio
import logging

from ggbot.util.session import session_label

from ..base import Tool

logger = logging.getLogger("ggbot.tool.admin")

LEVEL_NAMES = {0: "禁止", 1: "受限", 2: "普通", 3: "特权", 4: "心腹", 5: "归属"}

LEVEL_ALIASES = {
    "禁止": 0, "拉黑": 0, "封禁": 0, "禁用": 0, "黑名单": 0, "踢出": 0,
    "受限": 1, "限制": 1, "只聊天": 1,
    "普通": 2, "标准": 2, "正常": 2,
    "特权": 3, "高级": 3,
    "心腹": 4, "管理": 4, "管理员": 4,
    "归属": 5, "主人": 5, "群主": 5, "拥有者": 5,
}

LEVEL_ABILITIES = {
    0: "消息直接丢弃，不回复、不入库",
    1: "只能聊天、发表情、查记忆",
    2: "加上联网搜索、发图/视频、写记忆、语音克隆",
    3: "加上只读文件、发邮件",
    4: "加上写文件",
    5: "拥有 4 的全部能力，并可调整他人权限",
}


def parse_level(value) -> int:
    if isinstance(value, bool):
        raise ValueError("等级必须是 0-5 或等级名称")
    if isinstance(value, int):
        level = value
    else:
        text = str(value or "").strip()
        if not text:
            raise ValueError("缺少等级：0-5 或 禁止/受限/普通/特权/心腹/归属")
        if text.isdigit():
            level = int(text)
        elif text in LEVEL_ALIASES:
            level = LEVEL_ALIASES[text]
        else:
            raise ValueError(f"无法识别的等级: {text}（可用 0-5 或等级名称）")
    if not 0 <= level <= 5:
        raise ValueError("等级范围是 0-5（6 为永久锁，只能在 Web 工具页设置）")
    return level


def _oid8(openid: str) -> str:
    return (openid or "")[:8].upper()


class ManagePermission(Tool):
    name = "manage_permission"
    min_level = 5
    data_name = "admin"
    description = (
        "调整成员在本机的权限等级（只有归属者可用）。"
        "action=grant 授予/升降级，action=revoke 收回（设为 0 禁止），"
        "action=list 查看成员与等级，action=help 查看等级含义和说法示例。"
        "target 可以填昵称、8 位 id 或完整 openid；昵称重名时会返回候选，不要猜。"
        "等级：0 禁止（消息被丢弃）/ 1 受限（只能聊天）/ 2 普通（联网、记忆、发图、语音）/ "
        "3 特权（读文件、发邮件）/ 4 心腹（写文件）/ 5 归属（管理权限）。"
        "对方只描述能力时按此换算，比如“让他能改文件”→4，“只能看文件”→3，"
        "“能发邮件”→3，“取消权限/拉黑”→0；说不清要哪一级就先问一句，或调 help 列出六级让对方选。"
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
                "description": "grant 用：0-5，或 禁止/受限/普通/特权/心腹/归属",
            },
            "note": {"type": "string", "description": "可选备注"},
        },
        "required": ["action"],
    }
    prompt_hint = (
        "归属者要授权/降权/拉黑/解禁时用 manage_permission，直接按昵称或 8 位 id 找人不要求 openid；"
        "对方只说能力（“能改文件”“只读”“拉黑”）就换算成等级；"
        "不确定成员或等级先 list/help；对方问“权限怎么分”时用 help 的内容回答。"
    )

    async def run(self, args: dict, ctx: dict) -> dict:
        actor = ctx.get("actor") or {}
        caller = int(actor.get("level") or 0)
        if caller < 5:
            return {"ok": False, "error": "权限管理仅限归属者（L5）"}
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
        if is_self and level < 5:
            result["warning"] = "你已降低自己的等级；需要恢复请在 Web 权限页修改"
        return result

    # ---------- 查询 ----------

    def _help(self) -> dict:
        return {
            "ok": True,
            "levels": [
                {"level": lv, "name": LEVEL_NAMES[lv], "ability": LEVEL_ABILITIES[lv]}
                for lv in range(6)
            ],
            "examples": [
                "“把小明设成特权” → grant target=小明 level=3",
                "“让阿泽能改文件” → grant target=阿泽 level=4",
                "“他只能看文件” → grant target=... level=3",
                "“让他能发邮件” → grant target=... level=3",
                "“取消小林的权限 / 拉黑他” → revoke target=小林",
                "“现在谁能用文件” → list（再按等级说明）",
            ],
            "note": "成员重名时先用 list 看 8 位 id，再用 id 指定；6 为永久锁，只能在 Web 工具页设置。",
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
        data = getattr(self.rt.cfg, "data", None) or {}
        p = data.get("permissions") if isinstance(data, dict) else None
        try:
            return max(0, min(5, int((p or {}).get("default_level", 2))))
        except (TypeError, ValueError):
            return 2

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
    return [ManagePermission(rt)]
