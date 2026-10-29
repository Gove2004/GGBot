"""表情包 Tool：按情绪标签从素材库挑图发送，支持用户投喂入库。

素材与索引在 tool/chat/emoji/（与代码同分类目录，整目录可迁移）：
- stickers/  内置素材
- user/      用户投喂并同意收藏的素材
- index.json 情绪标签索引（v2）

索引以「情绪标签」为主键，而不是自由文本含义：
- MOOD_ALIASES 把口语（笑死、服了、破防…）归一到固定情绪；
- 文件系统是素材真相：查询时自动把目录里未入索引的图片补进来，文件名里的情绪词
  （如 `无语_翻白眼.gif`）会自动识别成标签；文件删掉后索引条目自动失效；
- 未标注的素材仍可被随机/按文件名使用，用 sticker_save 可补标签。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from ..base import Tool
from ggbot.core.config import BASE_DIR
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools.sticker")

IMAGE_EXTS = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}

# 固定情绪词表：左侧是标准标签，右侧是口语别名（用于检索与文件名识别）
MOOD_ALIASES: dict[str, tuple[str, ...]] = {
    "开心": ("高兴", "快乐", "笑", "哈哈", "笑死", "好耶", "兴奋", "乐"),
    "无语": ("无奈", "服了", "翻白眼", "沉默", "呃", "汗"),
    "委屈": ("难过", "哭", "伤心", "可怜", "卖惨", "破防"),
    "生气": ("气", "怒", "哼", "不爽", "恼"),
    "惊讶": ("震惊", "惊", "没想到", "卧槽", "啊这"),
    "期待": ("等", "盼", "想要", "搓手", "馋"),
    "卖萌": ("可爱", "撒娇", "贴贴", "亲亲", "乖"),
    "敷衍": ("哦", "嗯", "随便", "懒得", "摆"),
    "得意": ("骄傲", "牛逼", "炫耀", "赢", "飘"),
    "感动": ("暖心", "谢谢", "爱了", "泪目"),
    "困": ("累", "睡", "困倦", "熬夜"),
    "吃瓜": ("围观", "看戏", "八卦", "热闹"),
    "打招呼": ("你好", "在吗", "早安", "晚安", "嗨"),
    "道谢": ("谢", "感谢", "多谢"),
    "鼓励": ("加油", "抱抱", "安慰", "别怕", "摸头"),
    "拒绝": ("不要", "不行", "算了", "告辞", "拒绝"),
}

_MOOD_LOOKUP = {
    alias: mood
    for mood, aliases in MOOD_ALIASES.items()
    for alias in (mood, *aliases)
}
_MOOD_WORDS = tuple(sorted(_MOOD_LOOKUP, key=len, reverse=True))


def _canonical_moods(raw) -> list[str]:
    """把口语/自由标签归一为标准情绪词，去重保序。"""
    if isinstance(raw, str):
        tokens = re.split(r"[\s,，、/;；|]+", raw)
    else:
        tokens = [str(x) for x in (raw or [])]
    out: list[str] = []
    for token in tokens:
        token = token.strip()
        if not token:
            continue
        mood = _MOOD_LOOKUP.get(token)
        if mood is None:
            for word in _MOOD_WORDS:
                if word in token:
                    mood = _MOOD_LOOKUP[word]
                    break
        mood = mood or token[:20]
        if mood not in out:
            out.append(mood)
    return out[:6]


class _StickerBase(Tool):
    data_name = "chat/emoji"
    default_config = {
        "enabled": True,
        "max_per_turn": 1,
        "min_interval_sec": 10,
        "max_user_stickers": 200,
    }
    prompt_hint = (
        "情绪类回复优先用表情：对方在笑、吐槽、无语、卖萌、道谢、打招呼，或你想表达语气时，"
        "先 send_sticker 再补一句文字（同一轮可一起调用），比干打字自然；一次最多一张，别刷屏。"
        "对方说正事时不要用表情打岔。只有用户明确投喂并同意收藏时才能调用 sticker_save。"
    )

    def __init__(self, rt):
        super().__init__(rt)
        self._index_cache: tuple[int, list[dict]] | None = None
        self._last_sent: dict[str, float] = {}
        self._recent: dict[str, deque] = {}

    @property
    def root(self) -> Path:
        return self.rt.data_dir(self.key)

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> list[dict]:
        try:
            mtime = self.index_path.stat().st_mtime_ns
        except OSError:
            return []
        if self._index_cache and self._index_cache[0] == mtime:
            return self._index_cache[1]
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            items = data.get("items", []) if isinstance(data, dict) else data
        except (OSError, ValueError) as e:
            logger.warning("表情索引读取失败: %s", e)
            items = []
        migrated = []
        for it in items:
            if not isinstance(it, dict) or not it.get("file"):
                continue
            moods = it.get("moods")
            if moods is None:
                moods = it.get("scenes") or ([it["meaning"]] if it.get("meaning") else [])
            migrated.append({
                "file": str(it["file"]),
                "moods": _canonical_moods(moods),
                "desc": str(it.get("desc") or it.get("meaning") or "")[:40],
                "source": str(it.get("source") or "builtin"),
                "added_at": it.get("added_at") or "",
            })
        self._index_cache = (mtime, migrated)
        return migrated

    def _save_index(self, items: list[dict]) -> None:
        payload = {"version": 2, "items": items}
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.index_path)
        self._index_cache = None

    def _scan_files(self) -> list[str]:
        out = []
        for sub in ("stickers", "user"):
            d = self.root / sub
            d.mkdir(parents=True, exist_ok=True)
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                    out.append(p.relative_to(self.root).as_posix())
        return out

    @staticmethod
    def _infer_moods(rel: str) -> list[str]:
        stem = Path(rel).stem.replace("_", " ").replace("-", " ")
        moods = []
        for token in re.split(r"\s+", stem):
            if not token:
                continue
            mood = _MOOD_LOOKUP.get(token)
            if mood is None:
                for word in _MOOD_WORDS:
                    if len(word) >= 2 and word in token:
                        mood = _MOOD_LOOKUP[word]
                        break
            if mood and mood not in moods:
                moods.append(mood)
        return moods

    def _sync_index(self) -> list[dict]:
        """以目录为准补齐/清理索引；新文件可按文件名自动识别情绪。"""
        items = self._load_index()
        known = {it["file"] for it in items}
        changed = False
        for rel in self._scan_files():
            if rel in known:
                continue
            items.append({
                "file": rel,
                "moods": self._infer_moods(rel),
                "desc": Path(rel).stem.replace("_", " ")[:40],
                "source": "user" if rel.startswith("user/") else "builtin",
                "added_at": datetime.now().isoformat(timespec="seconds"),
            })
            changed = True
        kept = [it for it in items if (self.root / it["file"]).is_file()]
        if len(kept) != len(items):
            changed = True
        if changed:
            self._save_index(kept)
        return kept

    def _available(self) -> list[dict]:
        out = []
        for it in self._sync_index():
            p = (self.root / it["file"]).resolve()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append({**it, "_path": p})
        return out

    @staticmethod
    def _score(item: dict, mood: str) -> int:
        if not mood:
            return 0
        want = set(_canonical_moods(mood))
        if not want:
            return 0
        have = set(_canonical_moods(item.get("moods") or []))
        score = 8 * len(want & have)
        text = (str(item.get("desc") or "") + " " + " ".join(item.get("moods") or [])).lower()
        q = mood.lower().strip()
        if q and q in text:
            score += 4
        for token in re.split(r"[\s,，、/;；]+", q):
            if token and token in text:
                score += 2
        return score


class SendSticker(_StickerBase):
    name = "send_sticker"
    description = (
        "从本地表情库挑一张合适的图发送，用来代替文字表达情绪。"
        "mood 用一两个词描述此刻的情绪/场景（如 开心、无语、委屈、生气、惊讶、卖萌、"
        "敷衍、得意、困、吃瓜、打招呼、道谢、鼓励、拒绝），系统按情绪标签自动挑图；"
        "也可用 name 指定素材文件名。一次回复最多一张"
    )
    parameters = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "description": "想表达的情绪/场景关键词"},
            "name": {"type": "string", "description": "可选：指定素材文件名（见 sticker_list）"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        items = self._available()
        if not items:
            return {"ok": False, "error": "表情库为空（tool/chat/emoji/stickers 未放入素材）"}
        sid = str(ctx.get("session_id") or "")
        now = time.monotonic()
        limit = max(1, int(self.config.get("max_per_turn", 1) or 1))
        if int(ctx.get("_sticker_count", 0)) >= limit:
            return {"ok": False, "error": f"本轮已发过表情，最多 {limit} 张"}
        gap = max(0.0, float(self.config.get("min_interval_sec", 0) or 0))
        if gap and now - self._last_sent.get(sid, 0.0) < gap:
            return {"ok": False, "error": "刚发过表情，缓一缓"}

        name = str(args.get("name") or "").strip()
        mood = str(args.get("mood") or "").strip()
        if name:
            picked = [it for it in items if Path(it["file"]).name == name] or [
                it for it in items if name in Path(it["file"]).name
            ]
        else:
            scored = sorted(((self._score(it, mood), it) for it in items),
                            key=lambda x: x[0], reverse=True)
            top = [it for s, it in scored[:3] if s > 0]
            if not top:
                top = [it for it in items if not it.get("moods")] or items
            recent = self._recent.setdefault(sid, deque(maxlen=12))
            fresh = [it for it in top if Path(it["file"]).name not in recent]
            picked = fresh or top
        if not picked:
            return {"ok": False, "error": "没有匹配的表情"}
        item = random.choice(picked)
        result = await self.rt.send_media(ctx, 1, str(item["_path"]), "表情")
        if result.get("ok"):
            ctx["_sticker_count"] = int(ctx.get("_sticker_count", 0)) + 1
            self._last_sent[sid] = now
            self._recent.setdefault(sid, deque(maxlen=12)).append(Path(item["file"]).name)
            result["moods"] = item.get("moods") or []
        return result


class StickerList(_StickerBase):
    name = "sticker_list"
    config_owner = False
    toggleable = False
    prompt_hint = ""
    description = "查看表情库中与情绪匹配的素材（最多返回 10 条），用于了解有哪些图可用"
    parameters = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "description": "情绪/场景关键词，留空则列出全部"},
            "limit": {"type": "integer", "description": "返回条数，默认 10"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        items = self._available()
        mood = str(args.get("mood") or "").strip()
        limit = max(1, min(20, int(args.get("limit", 10) or 10)))
        scored = sorted(((self._score(it, mood), it) for it in items),
                        key=lambda x: x[0], reverse=True)
        rows = []
        for score, it in scored:
            if mood and score <= 0:
                continue
            rows.append({
                "name": Path(it["file"]).name,
                "moods": it.get("moods") or [],
                "desc": it.get("desc") or "",
                "source": it.get("source") or "builtin",
            })
            if len(rows) >= limit:
                break
        return {"ok": True, "items": rows, "total": len(items)}


class StickerSave(_StickerBase):
    name = "sticker_save"
    min_level = 2
    config_owner = False
    toggleable = False
    prompt_hint = ""
    description = (
        "把用户明确投喂并同意收藏的图片存进表情库（仅限 temp 目录下的图片路径），"
        "并按情绪标签建索引。群友随手发的图未经同意不要收藏"
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "图片路径（上下文中的本地附件路径）"},
            "moods": {
                "type": "array",
                "items": {"type": "string"},
                "description": "情绪标签，如 [\"无语\",\"无奈\"]（会自动归一到标准情绪）",
            },
            "desc": {"type": "string", "description": "可选：一句话描述画面"},
        },
        "required": ["source", "moods"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        raw = str(args.get("source") or "").strip()
        moods = _canonical_moods(args.get("moods"))
        if not raw or not moods:
            return {"ok": False, "error": "source 与 moods 不能为空"}
        temp_root = Path(self.rt.cfg.temp_dir).resolve()
        p = Path(raw)
        if not p.is_absolute():
            project = BASE_DIR / p
            p = project if project.exists() else temp_root / p
        try:
            p = p.resolve()
        except OSError as e:
            return {"ok": False, "error": f"路径无效: {e}"}
        if not p.is_relative_to(temp_root):
            return {"ok": False, "error": "只能收藏 temp 目录下的图片"}
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTS:
            return {"ok": False, "error": "文件不存在或不是支持的图片格式"}

        items = self._sync_index()
        user_count = sum(1 for it in items if it.get("source") == "user")
        max_user = max(0, int(self.config.get("max_user_stickers", 200) or 0))
        if user_count >= max_user:
            return {"ok": False, "error": f"用户素材已达上限 {max_user}"}

        data = await asyncio.to_thread(p.read_bytes)
        digest = hashlib.sha1(data).hexdigest()[:8]
        fname = f"user_{int(time.time() * 1000)}_{digest}{p.suffix.lower()}"
        dest = self.root / "user" / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, data)

        desc = str(args.get("desc") or p.stem.replace("_", " "))[:40]
        items.append({
            "file": f"user/{fname}",
            "moods": moods,
            "desc": desc,
            "source": "user",
            "added_at": datetime.now().isoformat(timespec="seconds"),
        })
        self._save_index(items)
        logger.info("表情入库: %s (%s)", fname, "/".join(moods),
                    extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": True, "name": fname, "moods": moods, "note": "已收藏进表情库"}


def get_tools(rt):
    return [SendSticker(rt), StickerList(rt), StickerSave(rt)]
