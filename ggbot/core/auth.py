"""权限：发送者等级（0-5）与工具门槛（1-6）。

- 身份只从服务端状态解析：消息发送者 openid → actors 表；模型参数永远不参与鉴权。
- 工具校验做两次：按等级裁剪 schema（省 token 与误调），执行期再校验（安全边界）。
- 6 为永久锁：任何发送者都达不到，工具门槛设为 6 后不会下发给任何人。
"""

from __future__ import annotations

LEVEL_NAMES = {0: "禁止", 1: "受限", 2: "普通", 3: "特权", 4: "心腹", 5: "归属"}
LEVEL_MAX = 5
LEVEL_LOCKED = 6
DEFAULT_LEVEL = 2


def clamp_level(value, lo: int = 0, hi: int = LEVEL_MAX) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, n))


def level_name(level) -> str:
    try:
        n = int(level)
    except (TypeError, ValueError):
        return "禁止"
    return LEVEL_NAMES.get(n, f"L{n}")


class Permissions:
    """发送者等级解析与维护（底层存 SQLite actors 表）。"""

    def __init__(self, cfg, storage):
        self.storage = storage
        data = getattr(cfg, "data", None) or {}
        p = data.get("permissions") if isinstance(data, dict) else None
        p = p if isinstance(p, dict) else {}
        self.default_level = clamp_level(p.get("default_level", DEFAULT_LEVEL))
        self.owners = [str(x).strip() for x in (p.get("owners") or []) if str(x).strip()]
        self._bootstrap()

    def _bootstrap(self) -> None:
        """配置里的 owners 首次启动时补成 5 级；已有记录不覆盖（面板改过就听面板的）。"""
        getter = getattr(self.storage, "get_actor", None)
        setter = getattr(self.storage, "set_actor", None)
        if not (getter and setter):
            return
        for openid in self.owners:
            if getter(openid) is None:
                setter(openid, LEVEL_MAX, note="config owner")

    def resolve(self, openid: str | None, name: str | None = None) -> dict:
        """消息发送者 → 权限主体；未登记的按 default_level，空 id 视为禁止。"""
        openid = (openid or "").strip()
        if not openid:
            return {"openid": "", "name": name or "", "level": 0, "known": False}
        getter = getattr(self.storage, "get_actor", None)
        row = getter(openid) if getter else None
        if row is None:
            return {
                "openid": openid,
                "name": name or "",
                "level": self.default_level,
                "known": False,
            }
        return {
            "openid": openid,
            "name": row.get("name") or name or "",
            "level": clamp_level(row.get("level")),
            "known": True,
        }

    def set_level(
        self, openid: str, level: int, note: str | None = None, name: str | None = None
    ) -> dict:
        openid = (openid or "").strip()
        if not openid:
            raise ValueError("openid 不能为空")
        setter = getattr(self.storage, "set_actor", None)
        if setter is None:
            raise RuntimeError("存储未就绪")
        return setter(openid, clamp_level(level), name=name, note=note)

    def members(self, limit: int = 200) -> list[dict]:
        """权限页数据：显式设置的成员 + 最近发言者，按等级/显式/活跃排序。"""
        list_actors = getattr(self.storage, "list_actors", None)
        list_senders = getattr(self.storage, "list_senders", None)
        out: dict[str, dict] = {}
        for row in (list_actors() if list_actors else []):
            openid = row.get("openid") or ""
            if not openid:
                continue
            out[openid] = {
                "openid": openid,
                "name": row.get("name") or "",
                "level": clamp_level(row.get("level")),
                "note": row.get("note") or "",
                "explicit": True,
                "messages": 0,
                "last_ts": "",
            }
        for s in (list_senders(limit) if list_senders else []):
            openid = s.get("sender") or ""
            if not openid:
                continue
            item = out.get(openid)
            if item is None:
                item = out[openid] = {
                    "openid": openid,
                    "name": s.get("sender_name") or "",
                    "level": self.default_level,
                    "note": "",
                    "explicit": False,
                    "messages": 0,
                    "last_ts": "",
                }
            item["name"] = item["name"] or (s.get("sender_name") or "")
            item["messages"] = int(s.get("messages") or 0)
            item["last_ts"] = s.get("last_ts") or ""
        rows = list(out.values())
        rows.sort(key=lambda r: r["last_ts"] or "", reverse=True)
        rows.sort(key=lambda r: (-r["level"], 0 if r["explicit"] else 1))
        return rows
