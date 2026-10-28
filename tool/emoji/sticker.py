"""表情包 Tool：按情绪/场景从素材库挑图发送，支持用户授权投喂入库。

素材与索引持久化在 tool/emoji/（与代码同目录）：
- stickers/  内置素材（clean_*.gif/jpg/png）
- user/      用户明确投喂并同意收藏的素材
- index.json 含义索引（文件名 -> 含义 -> 场景）
运行期不产生额外文件；用户投喂的源图来自 temp/，复制入库后由 temp TTL 清理。
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
from pathlib import Path

from ..base import Tool
from ggbot.core.config import BASE_DIR
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools.sticker")

IMAGE_EXTS = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}


class _StickerBase(Tool):
    data_name = "emoji"
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
        return self.rt.data_dir("emoji")

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
        items = [it for it in items if isinstance(it, dict) and it.get("file")]
        self._index_cache = (mtime, items)
        return items

    def _save_index(self, items: list[dict]) -> None:
        payload = {"version": 1, "items": items}
        tmp = self.index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.index_path)
        self._index_cache = None

    def _available(self) -> list[dict]:
        out = []
        for it in self._load_index():
            p = (self.root / it["file"]).resolve()
            if p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                out.append({**it, "_path": p})
        return out

    @staticmethod
    def _score(item: dict, mood: str) -> int:
        if not mood:
            return 0
        q = mood.lower().strip()
        text = (str(item.get("meaning") or "") + " " + " ".join(item.get("scenes") or [])).lower()
        if q and q in text:
            return 8
        score = 0
        for token in re.split(r"[\s,，、/;；]+", q):
            if not token:
                continue
            if token in text:
                score += 4
            else:
                score += sum(1 for ch in set(token) if ch in text)
        return score


class SendSticker(_StickerBase):
    name = "send_sticker"
    description = (
        "从本地表情包素材库挑一张合适的图发送，用来代替文字表达情绪。"
        "mood 用一两个词描述此刻想表达的情绪/场景（如 开心、无语、委屈、期待、惊讶、卖萌、"
        "吐槽、盯人、卖惨、夸赞、道谢、打招呼），系统会按含义索引自动挑图；"
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
            return {"ok": False, "error": "表情库为空（tool/emoji/stickers 未放入素材）"}
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
            top = [it for s, it in scored[:3] if s > 0] or [
                it for it in items if "通用" in (it.get("scenes") or [])
            ] or items
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
            result["meaning"] = item.get("meaning") or ""
        return result


class StickerList(_StickerBase):
    name = "sticker_list"
    config_owner = False
    toggleable = False
    prompt_hint = ""
    description = "查看表情库中与情绪/场景匹配的素材（最多返回 10 条），用于了解有哪些图可用"
    parameters = {
        "type": "object",
        "properties": {
            "mood": {"type": "string", "description": "情绪/场景关键词，留空则列通用素材"},
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
            if mood and score <= 0 and len(rows) >= limit:
                break
            rows.append({
                "name": Path(it["file"]).name,
                "meaning": it.get("meaning") or "",
                "scenes": it.get("scenes") or [],
                "source": it.get("source") or "builtin",
            })
            if len(rows) >= limit:
                break
        return {"ok": True, "items": rows, "total": len(items)}


class StickerSave(_StickerBase):
    name = "sticker_save"
    config_owner = False
    toggleable = False
    prompt_hint = ""
    description = (
        "把用户明确投喂并同意收藏的图片存进表情库（仅限 temp 目录下的图片路径），"
        "同时记录含义与适用场景。群友随手发的图未经同意不要收藏"
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "图片路径（上下文中的本地附件路径）"},
            "meaning": {"type": "string", "description": "这张图表达的含义，如「无语」"},
            "scenes": {
                "type": "array",
                "items": {"type": "string"},
                "description": "适用场景关键词，如 [\"尴尬\",\"无奈\"]",
            },
        },
        "required": ["source", "meaning"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        raw = str(args.get("source") or "").strip()
        meaning = str(args.get("meaning") or "").strip()
        if not raw or not meaning:
            return {"ok": False, "error": "source 与 meaning 不能为空"}
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

        items = self._load_index()
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

        scenes = args.get("scenes") or []
        if isinstance(scenes, str):
            scenes = [s.strip() for s in re.split(r"[,，、/;；]+", scenes) if s.strip()]
        items.append({
            "file": f"user/{fname}",
            "meaning": meaning[:40],
            "scenes": [str(s)[:20] for s in scenes][:8],
            "source": "user",
        })
        self._save_index(items)
        logger.info("表情入库: %s (%s)", fname, meaning[:20],
                    extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": True, "name": fname, "note": "已收藏进表情库"}


def get_tools(rt):
    return [SendSticker(rt), StickerList(rt), StickerSave(rt)]
