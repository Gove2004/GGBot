"""表情包 Tool：模型说一句想表达什么，向量在表情库里找最贴的一张发出去。

素材与索引都在 tool/media/emoji/（整目录复制即可迁移）：
- stickers/  手动放入的素材
- user/      用户投喂并同意收藏的素材
- index.json 素材库索引 v3：{file, desc, source, added_at}（人看的、可手改、入库）
- vectors.json 描述向量缓存：{file: {desc, vec}}，不入库；缺失或描述改过会按新 desc 重算

挑图不再靠情绪词表：把「笑死」归一成「开心」这类判断本来就该模型自己做，
固定词表只会在模型没想到的说法上失灵。现在每条素材只有一句话描述 desc
（emoji_save 收藏时由识图自动写，手动放进目录的先用文件名兜底），
检索时把 query 和 desc 都 embedding，按余弦相似度取最贴的；
没配 embedding 或向量缺失时降级成字面重合度匹配（复用记忆检索的二元组算法）。

文件系统是素材真相：目录里多出来的图会被补进索引，被删掉的条目自动失效。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from ..base import Tool
from ggbot.core.config import BASE_DIR
from ggbot.llm.memory import bigrams
from ggbot.util.logging_setup import clip
from ggbot.util.session import session_label

logger = logging.getLogger("ggbot.tools.emoji")

IMAGE_EXTS = {".gif", ".jpg", ".jpeg", ".png", ".webp", ".bmp"}
DATA_KEY = "media/emoji"
RECENT_KEEP = 8         # 每个会话记住最近发过的张数，避免连着重复
NEAR_TIE = 0.05         # 与最高分相差不到这个数的才算同一档，随机只在这档里挑
DESC_MAX = 120

DEFAULT_CONFIG = {
    "enabled": True,
    "max_per_turn": 1,
    "min_interval_sec": 10,
    "max_user_stickers": 200,
    "min_score": 0.50,
    # 检索侧的 instruct：Qwen3-Embedding 这类模型要求 query 带任务说明，文档侧不带。
    # 实测同一批素材，加了之后 5 个说法命中 4 个（不加只有 3 个），换成别的厂商可改文案或留空。
    "query_instruct": "Given a Chinese chat message, retrieve the sticker description "
                      "that matches the feeling to send.",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _stem_desc(name: str) -> str:
    """没有描述时拿文件名兜底：下划线/连字符当空格，够关键词匹配用了。"""
    return Path(name).stem.replace("_", " ").replace("-", " ").strip()[:DESC_MAX]


def _encode_vec(vec: np.ndarray) -> str:
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode()


def _decode_vec(raw) -> np.ndarray | None:
    if not raw:
        return None
    try:
        vec = np.frombuffer(base64.b64decode(str(raw)), dtype=np.float32)
    except (ValueError, TypeError):
        return None
    return vec if vec.size else None


class EmojiLibrary:
    """表情库本体：索引读写、语义检索、投喂入库、发送限流。

    send_emoji / emoji_list / emoji_save 共用这一份状态（缓存与限流必须共享，
    否则三个工具各自的计数器互相看不见）。
    """

    def __init__(self, rt):
        self.rt = rt
        self._cache: tuple[tuple[int, int], list[dict]] | None = None  # (索引/向量 mtime, 解析结果)
        self._last_sent: dict[str, float] = {}
        self._recent: dict[str, deque] = {}

    # ---------- 目录与配置 ----------

    @property
    def root(self) -> Path:
        return self.rt.data_dir(DATA_KEY)

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    @property
    def vectors_path(self) -> Path:
        return self.root / "vectors.json"

    @property
    def config(self) -> dict:
        return self.rt.load_config(DATA_KEY, DEFAULT_CONFIG)

    def image_path(self, rel: str) -> Path | None:
        """把 Web 传来的相对路径解析成素材文件；不在库里、不是图片或已被删掉都返回 None。"""
        if not rel or rel.startswith(("/", "\\")) or "\x00" in rel:
            return None
        root = self.root.resolve()
        try:
            p = (root / rel).resolve()
        except OSError:
            return None
        if p.parent != root and root not in p.parents:
            return None
        if p.suffix.lower() not in IMAGE_EXTS or not p.is_file():
            return None
        return p

    # ---------- 索引读写 ----------

    def _stamp(self) -> tuple[int, int]:
        def ns(p: Path) -> int:
            try:
                return p.stat().st_mtime_ns
            except OSError:
                return -1
        return ns(self.index_path), ns(self.vectors_path)

    def _load(self) -> dict[str, dict]:
        """索引 + 向量缓存，按两个文件的 mtime 复用解析结果（向量整包不小）。"""
        stamp = self._stamp()
        if stamp[0] < 0:
            return {}
        if self._cache and self._cache[0] == stamp:
            return {it["file"]: it for it in self._cache[1]}
        items = self._parse()
        vecs = self._parse_vectors()
        for it in items:
            hit = vecs.get(it["file"])
            if hit is not None and hit[0] == it["desc"]:
                it["vec"] = hit[1]
        self._cache = (stamp, items)
        return {it["file"]: it for it in items}

    def _parse(self) -> list[dict]:
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            logger.warning("表情索引读取失败，按空库重建: %s", e)
            return []
        rows = data.get("items", []) if isinstance(data, dict) else data
        out: list[dict] = []
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, dict):
                continue
            rel = str(raw.get("file") or "")
            if not rel or ".." in Path(rel).parts:
                continue  # 手改出来的越界路径一律不认
            desc = str(raw.get("desc") or "")
            if not desc:
                # v2 索引只有 moods 词表：拼成一句话当描述，向量重算
                desc = " ".join(str(m) for m in (raw.get("moods") or []) if m)
            out.append({
                "file": rel,
                "desc": desc[:DESC_MAX].strip(),
                "source": str(raw.get("source")
                              or ("user" if rel.startswith("user/") else "builtin")),
                "added_at": str(raw.get("added_at") or ""),
                "vec": None,
            })
        return out

    def _parse_vectors(self) -> dict[str, tuple[str, np.ndarray]]:
        """向量缓存：{文件: (生成时的描述, 向量)}——描述改了，旧向量就作废。"""
        try:
            data = json.loads(self.vectors_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            return {}  # 缓存文件坏了就当没有，随后按 desc 重算
        rows = data.get("vectors", data) if isinstance(data, dict) else {}
        out: dict[str, tuple[str, np.ndarray]] = {}
        for rel, raw in (rows.items() if isinstance(rows, dict) else []):
            if not isinstance(raw, dict):
                continue
            vec = _decode_vec(raw.get("vec"))
            if vec is not None:
                out[str(rel)] = (str(raw.get("desc") or ""), vec)
        return out

    def _save(self, items: list[dict]) -> None:
        """索引只留人看得懂的部分；向量单独放 vectors.json（体积大、可随时重建）。"""
        rows = [{"file": it["file"], "desc": it["desc"], "source": it["source"],
                 "added_at": it["added_at"]} for it in items]
        self._write_json(self.index_path, {"version": 3, "items": rows})
        known = {it["file"] for it in items}
        vecs = {rel: {"desc": desc, "vec": _encode_vec(v)}
                for rel, (desc, v) in self._parse_vectors().items() if rel in known}
        for it in items:
            if it.get("vec") is not None:
                vecs[it["file"]] = {"desc": it["desc"], "vec": _encode_vec(it["vec"])}
        self._write_json(self.vectors_path, {"vectors": vecs})
        self._cache = (self._stamp(), items)

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def _scan(self) -> list[str]:
        root = self.root
        found = []
        for sub in ("stickers", "user"):
            d = root / sub
            d.mkdir(parents=True, exist_ok=True)
            for p in sorted(d.rglob("*")):
                if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("."):
                    found.append(p.relative_to(root).as_posix())
        return found

    # ---------- 向量 ----------

    @property
    def _memory(self):
        mem = self.rt.memory
        return mem if mem is not None and getattr(mem, "enabled", False) else None

    async def _embed(self, texts: list[str]) -> list[np.ndarray]:
        """文本 → 向量；没配 embedding 或调用失败就返回空（检索自动降级为关键词）。"""
        mem = self._memory
        if mem is None or not texts:
            return []
        try:
            return await asyncio.to_thread(mem.embed_many, texts)
        except Exception as e:  # noqa: BLE001
            logger.warning("表情向量生成失败，改用关键词匹配: %s", clip(str(e), 60))
            return []

    def _query_text(self, q: str) -> str:
        """query 侧包一层 instruct（文档侧不包）：Qwen3-Embedding 按这个格式做检索。"""
        instr = str(self.config.get("query_instruct") or "").strip()
        return f"Instruct: {instr}\nQuery: {q}" if instr else q

    # ---------- 同步 ----------

    async def items(self) -> list[dict]:
        """可用素材：目录是真相——多出来的图当场进索引，删掉的条目自动失效。"""
        indexed = await asyncio.to_thread(self._load)
        found = await asyncio.to_thread(self._scan)
        items, added = [], []
        for rel in found:
            it = indexed.get(rel)
            if it is None:
                it = {"file": rel, "desc": _stem_desc(rel),
                      "source": "user" if rel.startswith("user/") else "builtin",
                      "added_at": _now(), "vec": None}
                added.append(it)
            items.append(it)
        pending = [it for it in items if it.get("vec") is None and it["desc"]]
        filled = 0
        if pending and self._memory is not None:
            for it, vec in zip(pending, await self._embed([it["desc"] for it in pending])):
                it["vec"] = vec
                filled += 1
        if added or filled or len(items) != len(indexed):
            await asyncio.to_thread(self._save, items)
        return items

    # ---------- 检索 ----------

    async def match(self, query: str, limit: int, items: list[dict] | None = None) -> list[dict]:
        """按语义找素材，返回带 score 的条目（已按相似度排序、按 min_score 过滤）。"""
        if items is None:
            items = await self.items()
        if not items:
            return []
        q = (query or "").strip()
        if not q:
            return [{**it, "score": 0.0} for it in items[:limit]]
        if self._memory is not None and any(it.get("vec") is not None for it in items):
            got = await self._embed([self._query_text(q)])
            if got:
                return self._cosine(items, got[0], limit)
        return self._keyword(items, q, limit)

    def _cosine(self, items: list[dict], qv: np.ndarray, limit: int) -> list[dict]:
        qn = float(np.linalg.norm(qv))
        floor = float(self.config.get("min_score", 0.50) or 0)
        rows = []
        for it in items:
            vec = it.get("vec")
            if vec is None:
                continue  # 向量缺失的条目不参与相似度排序，避免瞎给
            denom = qn * float(np.linalg.norm(vec))
            if denom < 1e-9:
                continue
            score = float(np.dot(qv, vec)) / denom
            if score >= floor:
                rows.append({**it, "score": round(score, 4)})
        rows.sort(key=lambda r: -r["score"])
        return rows[:limit]

    @staticmethod
    def _keyword(items: list[dict], q: str, limit: int) -> list[dict]:
        """降级匹配：字面二元组重合度（各家的向量分布差很多，这里不设阈值）。"""
        qgrams = bigrams(q.lower())
        if not qgrams:
            return []
        rows = []
        for it in items:
            text = f"{it['desc']} {Path(it['file']).stem}".lower()
            hit = len(qgrams & bigrams(text))
            if hit:
                rows.append({**it, "score": round(hit / len(qgrams), 4)})
        rows.sort(key=lambda r: -r["score"])
        return rows[:limit]

    # ---------- 发送 ----------

    async def send(self, args: dict, ctx: dict) -> dict:
        cfg = self.config
        sid = str(ctx.get("session_id") or "")
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "error": "query 不能为空：说清这一刻想表达什么"}
        limit = max(1, int(cfg.get("max_per_turn", 1) or 1))
        if int(ctx.get("_emoji_count", 0)) >= limit:
            return {"ok": False, "error": f"本轮已发过表情，最多 {limit} 张"}
        gap = max(0.0, float(cfg.get("min_interval_sec", 10) or 0))
        now = time.monotonic()
        if gap and now - self._last_sent.get(sid, 0.0) < gap:
            return {"ok": False, "error": f"刚发过表情，{int(gap)}s 内别再发"}

        items = await self.items()
        if not items:
            return {"ok": False, "error": "表情库为空（素材放 tool/media/emoji/stickers/，"
                                          "或用 emoji_save 收藏用户投喂的图）"}
        rows = await self.match(query, 6, items)
        if not rows:
            return {"ok": False, "error": "库里没有贴近这个说法的表情，直接说话就好"}
        recent = self._recent.setdefault(sid, deque(maxlen=RECENT_KEEP))
        # 只在「分数贴着最高分的那一小撮」里随机，保留换着发的趣味；
        # 差一档的候选不参与，否则随机会把明显更贴的那张换掉
        best = rows[0]["score"]
        top = [r for r in rows if r["score"] >= best - NEAR_TIE]
        picked = random.choice([r for r in top if r["file"] not in recent] or top)
        path = self.root / picked["file"]
        if not path.is_file():
            # 文件被删了：下次 items() 扫目录就会把这条剔掉，这次先如实报错
            return {"ok": False, "error": f"素材文件已不存在: {picked['file']}"}

        result = await self.rt.send_media(ctx, 1, str(path), "表情")
        if result.get("ok"):
            ctx["_emoji_count"] = int(ctx.get("_emoji_count", 0)) + 1
            self._last_sent[sid] = now
            recent.append(picked["file"])
            # 把发出去的那张长什么样回给模型：它下一轮才知道库里有什么、该怎么说
            result["emoji"] = {"name": Path(picked["file"]).name,
                                 "desc": picked["desc"], "score": picked["score"]}
        return result

    # ---------- 入库 ----------

    async def describe(self, path: Path, session: str | None) -> str:
        """识图写描述；没开识图或识别失败返回空串，由调用方兜底。"""
        vision = getattr(self.rt.bot, "vision", None)
        if vision is None or not getattr(vision, "enabled", False):
            return ""
        try:
            text = await asyncio.to_thread(vision.describe, path, session)
        except Exception as e:  # noqa: BLE001
            logger.warning("表情识图失败: %s", clip(str(e), 60),
                           extra={"session": session_label(session)})
            return ""
        return " ".join(str(text or "").split())[:DESC_MAX]

    async def save(self, args: dict, ctx: dict) -> dict:
        raw = str(args.get("source") or "").strip()
        if not raw:
            return {"ok": False, "error": "source 不能为空（temp 目录下的图片路径）"}
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

        items = await self.items()
        data = await asyncio.to_thread(p.read_bytes)
        digest = hashlib.sha1(data).hexdigest()[:12]
        name = f"user_{digest}{p.suffix.lower()}"
        rel = f"user/{name}"
        if any(it["file"] == rel for it in items):
            return {"ok": True, "name": name, "note": "这张已经在库里了"}

        user_count = sum(1 for it in items if it["source"] == "user")
        max_user = max(0, int(self.config.get("max_user_stickers", 200) or 0))
        if user_count >= max_user:
            return {"ok": False, "error": f"用户素材已达上限 {max_user} 张，先删一些"}

        desc = str(args.get("desc") or "").strip() or await self.describe(
            p, session_label(ctx.get("session_id"))) or _stem_desc(p.name)
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(dest.write_bytes, data)
        vecs = await self._embed([desc])
        entry = {"file": rel, "desc": desc[:DESC_MAX].strip(), "source": "user",
                 "added_at": _now(), "vec": vecs[0] if vecs else None}
        await asyncio.to_thread(self._save, [*items, entry])
        logger.info("表情入库 %s（%s）", name, clip(desc, 40),
                    extra={"session": session_label(ctx.get("session_id"))})
        return {"ok": True, "name": name, "desc": entry["desc"], "note": "已收藏进表情库"}

    # ---------- 浏览 ----------

    async def list_items(self, args: dict) -> dict:
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
        query = str(args.get("query") or "").strip()
        items = await self.items()
        rows = await self.match(query, limit, items)
        return {"ok": True, "total": len(items),
                "items": [{"name": Path(r["file"]).name, "desc": r["desc"],
                           "source": r["source"], **({"score": r["score"]} if query else {})}
                          for r in rows]}


class _EmojiTool(Tool):
    data_name = DATA_KEY
    default_config = DEFAULT_CONFIG

    def __init__(self, rt, lib: EmojiLibrary):
        super().__init__(rt)
        self.lib = lib


class SendEmoji(_EmojiTool):
    name = "send_emoji"
    description = (
        "从本地表情库挑最贴的一张发出去，用来说语气、代替干打字。"
        "query 写一句你想表达的东西+想要的画面（如「无语到想翻白眼」「恭喜对方，热烈一点」），"
        "系统按语义匹配；库里没有贴近的会直接返回失败，那时说话就行，别硬凑或反复换词试。"
        "一次回复最多一张，同一句正文可以照发；对方在说正事时别用表情打岔"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "这一刻想表达的情绪/场景，一句话说清"},
        },
        "required": ["query"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.lib.send(args, ctx)


class EmojiList(_EmojiTool):
    name = "emoji_list"
    config_owner = False
    toggleable = False
    description = (
        "看看表情库里有什么素材。query 留空按入库时间列出全部（最多 limit 条），"
        "给了 query 就按语义相似度排序并带上匹配分"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "可选：想表达的内容，用于相似度排序"},
            "limit": {"type": "integer", "description": "返回条数，默认 20，最多 50"},
        },
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.lib.list_items(args)


class EmojiSave(_EmojiTool):
    name = "emoji_save"
    min_level = 2
    config_owner = False
    toggleable = False
    description = (
        "把用户明确投喂并同意收藏的图片收进表情库（只接受 temp 目录下的图片路径）。"
        "描述默认由识图自动生成，只在识图没开或识别不准时传 desc 覆盖。"
        "群友随手发的图未经同意不要收藏"
    )
    parameters = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "图片路径（上下文里的本地附件路径）"},
            "desc": {"type": "string", "description": "可选：一句话说清画面和适合什么场合"},
        },
        "required": ["source"],
    }

    async def run(self, args: dict, ctx: dict) -> dict:
        return await self.lib.save(args, ctx)


def get_tools(rt):
    lib = EmojiLibrary(rt)
    return [SendEmoji(rt, lib), EmojiList(rt, lib), EmojiSave(rt, lib)]
