"""键值库工具：把每次汇报的数据留下来，下次能对比、能算增量、能取出一条日序列。

四个动作共用 tool/kvdb/config.json 与 tool/kvdb/kv.db（见 _kv_store.py）。
命名空间由服务端按 ctx.session_id 定，模型无从指定，所以跨会话读不到别人的数据。
"""

from __future__ import annotations

import asyncio

from ..base import Tool
from ggbot.util.logging_setup import clip
from ggbot.util.text import to_str
from ._kv_store import DATA_KEY, KVStore

DEFAULT_CONFIG = {
    "enabled": True,
    "max_keys": 500,          # 每个会话最多存几条：超了要先 kv_del，不静默挤掉旧数据
    "max_key_chars": 100,
    "max_value_chars": 4000,
    "max_list_items": 50,
    "list_value_chars": 200,  # 列表里每条值只给这么长（看全值用 kv_get），免得一次撑爆上下文
}


class _KVTool(Tool):
    data_name = DATA_KEY
    default_config = DEFAULT_CONFIG
    min_level = 2

    def __init__(self, rt, store: KVStore):
        super().__init__(rt)
        self.store = store

    def _ns(self, ctx: dict) -> tuple[str, str]:
        """命名空间 = 会话；拿不到就不许读写（没有会话的数据无处归属）。"""
        ns = str(ctx.get("session_id") or "").strip()
        return (ns, "") if ns else ("", "缺少会话标识，无法读写键值库")

    def _key(self, args: dict) -> tuple[str, str]:
        key = str(args.get("key") or "").strip()
        if not key:
            return "", "key 不能为空"
        if "\x00" in key or "\n" in key or "\r" in key:
            return "", "key 不能含换行或空字符"
        cap = max(1, int(self.config.get("max_key_chars", 100) or 100))
        if len(key) > cap:
            return "", f"key 太长（最多 {cap} 字），起短一点"
        return key, ""


class KVSet(_KVTool):
    name = "kv_set"
    description = (
        "往本会话的键值库存一条数据（重启不丢，别的会话看不到），用来记住每次汇报的数值、"
        "做天天对比和增量。键要起得有规律：按天存就带日期（`网站价格:2026-11-06`，"
        "日期先调 time 拿，别凭记忆猜），同一个指标长期只用一个键。"
        "返回里带 old（被覆盖前的旧值），要算增量看它就够，不用再 kv_get 一次。"
        "value 可以是数字，也可以是一段 JSON 文本（多个字段一起存）"
    )
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "键名，如 网站价格:2026-11-06"},
            "value": {"type": "string", "description": "要存的值（数字或 JSON 文本）"},
        },
        "required": ["key", "value"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        ns, err = self._ns(ctx)
        if err:
            return {"ok": False, "error": err}
        key, err = self._key(args)
        if err:
            return {"ok": False, "error": err}
        if args.get("value") is None:
            return {"ok": False, "error": "value 不能为空（要删掉这条用 kv_del）"}
        value = to_str(args["value"])
        cap = max(1, int(self.config.get("max_value_chars", 4000) or 4000))
        if len(value) > cap:
            return {"ok": False, "error": f"value 太长（{len(value)} 字，最多 {cap} 字），只存要对比的那几个字段"}
        cfg = self.config
        exists = await asyncio.to_thread(self.store.get, ns, key)
        if exists is None:
            total = await asyncio.to_thread(self.store.count, ns)
            max_keys = max(1, int(cfg.get("max_keys", 500) or 500))
            if total >= max_keys:
                return {"ok": False, "error": f"本会话已存 {total} 条（上限 {max_keys}），先 kv_del 清掉不要的旧键"}
        res = await asyncio.to_thread(self.store.put, ns, key, value)
        return {"ok": True, "key": key, "value": value, "old": res["old"],
                "created": res["created"], "updated_at": res["updated_at"],
                "note": "已存进本会话的键值库" + ("（新建）" if res["created"] else "（覆盖了旧值，old 就是被覆盖前的）")}


class KVGet(_KVTool):
    name = "kv_get"
    config_owner = False
    toggleable = False
    description = (
        "读本会话键值库里的一条（kv_set 存进去的）。要对比上次的数据、算增量、"
        "取回某天的记录时调用；found=false 就是还没存过，别把「没存过」当成 0"
    )
    parameters = {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "键名"}},
        "required": ["key"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        ns, err = self._ns(ctx)
        if err:
            return {"ok": False, "error": err}
        key, err = self._key(args)
        if err:
            return {"ok": False, "error": err}
        row = await asyncio.to_thread(self.store.get, ns, key)
        if row is None:
            return {"ok": True, "key": key, "found": False, "value": None,
                    "note": "本会话没存过这个键"}
        return {"ok": True, "key": key, "found": True, "value": row["value"],
                "updated_at": row["updated_at"]}


class KVList(_KVTool):
    name = "kv_list"
    config_owner = False
    toggleable = False
    description = (
        "按前缀列出本会话存过的键值，按键名字典序排（带日期的键就是时间序），"
        "用来取一条日序列看趋势、算这段时间的总增量。prefix 留空就是列出全部。"
        "列表里每条值只给前一小段，要看全值用 kv_get"
    )
    parameters = {
        "type": "object",
        "properties": {
            "prefix": {"type": "string", "description": "可选：键名前缀，如 网站价格: 取这个指标的所有日期"},
            "limit": {"type": "integer", "description": "可选：返回条数，默认 20"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        ns, err = self._ns(ctx)
        if err:
            return {"ok": False, "error": err}
        cfg = self.config
        cap = max(1, int(cfg.get("max_list_items", 50) or 50))
        try:
            limit = max(1, min(cap, int(args.get("limit", 20) or 20)))
        except (TypeError, ValueError):
            limit = min(20, cap)
        prefix = str(args.get("prefix") or "").strip()
        rows = await asyncio.to_thread(self.store.list, ns, prefix, limit)
        width = max(1, int(cfg.get("list_value_chars", 200) or 200))
        total = await asyncio.to_thread(self.store.count, ns)
        return {"ok": True, "total": total, "shown": len(rows), "prefix": prefix,
                "items": [{"key": r["key"], "value": clip(r["value"], width),
                           "updated_at": r["updated_at"]} for r in rows],
                "note": f"本会话共 {total} 条，这次给出 {len(rows)} 条（按键名排序）"}


class KVDel(_KVTool):
    name = "kv_del"
    config_owner = False
    toggleable = False
    description = (
        "删掉本会话键值库里的一条。存错了、或者旧数据不再需要时调用；"
        "键不存在也返回成功（本来就没有）。删掉就找不回来，别拿它清整批数据"
    )
    parameters = {
        "type": "object",
        "properties": {"key": {"type": "string", "description": "键名"}},
        "required": ["key"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        ns, err = self._ns(ctx)
        if err:
            return {"ok": False, "error": err}
        key, err = self._key(args)
        if err:
            return {"ok": False, "error": err}
        gone = await asyncio.to_thread(self.store.delete, ns, key)
        return {"ok": True, "key": key, "deleted": gone,
                "note": "已删除" if gone else "本来就没有这个键"}


def get_tools(rt):
    store = KVStore(rt)
    return [KVSet(rt, store), KVGet(rt, store), KVList(rt, store), KVDel(rt, store)]