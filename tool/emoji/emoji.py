"""表情包 Tool：模型说一句想表达什么，向量在表情库里找最贴的一张发出去。

这一层只做编排（限流、发送、入库、面板动作），另外两件事各自成文件：
- `_emoji_store.py`：素材目录与 index.json / vectors.json / usage.json 的读写，检索不参与
- `_emoji_match.py`：query 与素材描述怎么比、同档里怎么选

素材都在 `tool/emoji/`（整目录复制即可迁移，代码和素材同目录）：图片直接放这个目录，旧机器上的
`stickers/`、`user/` 子目录里的图也照样算同一个库，目录就是唯一真相。

挑图不靠情绪词表：把「笑死」归一成「开心」这类判断本来就该模型自己做，固定词表只会在
模型没想到的说法上失灵。每条素材只有一句人写的描述 `desc`（`save_emoji` 与面板导入由识图自动写，
手动放进目录的先用文件名兜底，面板上可以直接改），检索时 query 和 desc 都 embedding 比余弦；
没配 embedding 或向量缺失时降级成字面二元组匹配。发出去一次记一笔使用统计，同档候选里
优先换着发用得少的，而不是纯随机。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import deque
from pathlib import Path

from ..base import Tool
from ggbot.core.config import BASE_DIR
from ggbot.util.logging_setup import clip
from ggbot.util.session import session_label
from ._emoji_match import EmojiMatcher
from ._emoji_store import (
    DATA_KEY,
    DEFAULT_CONFIG,
    DESC_MAX,
    IMAGE_EXTS,
    MAX_IMAGE_BYTES,
    EmojiStore,
    _stem_desc,
)

logger = logging.getLogger("ggbot.tools.emoji")

RECENT_KEEP = 8        # 每个会话记住最近发过的张数，避免连着重复
CANDIDATE_POOL = 8     # 一次挑图最多看几张候选（同档随机就在这几张里进行）


class EmojiLibrary:
    """表情库本体：把 store（文件）和 matcher（挑图）串成工具与面板要的那几个动作。

    send_emoji / save_emoji / list_emoji 共用这一份状态（限流计数器必须共享，
    否则三个工具各自的计数互相看不见）。
    """

    max_image_bytes = MAX_IMAGE_BYTES   # 面板上传要照着这个数拦，别让它自己再写一份

    def __init__(self, rt):
        self.rt = rt
        self.store = EmojiStore(rt)
        self.matcher = EmojiMatcher(self.store)
        self._last_sent: dict[str, float] = {}
        self._recent: dict[str, deque] = {}

    # ---------- 对外的小口子 ----------

    @property
    def config(self) -> dict:
        return self.store.config

    @property
    def root(self) -> Path:
        return self.store.root

    def image_path(self, rel: str) -> Path | None:
        return self.store.image_path(rel)

    async def rows(self, force: bool = False) -> list[dict]:
        """只读视图：面板浏览走这里，不写盘、不嵌入（一进页面就重算向量是要卡死的）。"""
        return await asyncio.to_thread(self.store.rows, force)

    async def items(self) -> list[dict]:
        """聊天与写入动作用：只读视图 + 当场把索引补齐、把缺的向量算出来。"""
        rows = await asyncio.to_thread(self.store.rows)
        await asyncio.to_thread(self.store.ensure, rows)
        pending = [r for r in rows if r["vec"] is None and r["desc"]]
        if pending:
            vecs = await self.matcher.embed([r["desc"] for r in pending])
            for r, vec in zip(pending, vecs):
                r["vec"] = vec
                self.store.put_vector(r["file"], r["desc"], vec)
        await asyncio.to_thread(self.store.flush)
        return rows

    # ---------- 挑与发 ----------

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

        rows = await self.items()
        if not rows:
            return {"ok": False, "error": "表情库为空（图片放 tool/emoji/，"
                                          "或用 save_emoji 收藏用户投喂的图）"}
        hits, near = await self.matcher.rank(query, rows, CANDIDATE_POOL)
        recent = self._recent.setdefault(sid, deque(maxlen=RECENT_KEEP))
        picked = self.matcher.choose(hits, recent)
        if picked is None:
            # 相近的那几张一起回给模型：它看得见库里大致有什么说法，才不会换个同义词再试一次
            out = {"ok": False, "error": "库里没有贴近这个说法的表情，直接说话就好"}
            out["near"] = [{"name": Path(r["file"]).name, "desc": r["desc"],
                            "score": r["score"]} for r in near]
            return out
        path = self.store.root / picked["file"]
        if not path.is_file():
            # 文件被删了：下次扫目录就会把这条剔掉，这次先如实报错
            return {"ok": False, "error": f"素材文件已不存在: {picked['file']}"}

        result = await self.rt.send_media(ctx, 1, str(path), "表情")
        if result.get("ok"):
            ctx["_emoji_count"] = int(ctx.get("_emoji_count", 0)) + 1
            self._last_sent[sid] = now
            recent.append(picked["file"])
            await asyncio.to_thread(self._used, picked["file"])
            # 把发出去的那张长什么样回给模型：它下一轮才知道库里有什么、该怎么说
            result["emoji"] = {"name": Path(picked["file"]).name,
                               "desc": picked["desc"], "score": picked["score"]}
        return result

    def _used(self, rel: str) -> None:
        self.store.record_use(rel)
        self.store.flush()

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

    async def ingest(self, data: bytes, suffix: str, label: str,
                     desc_hint: str, session: str | None) -> dict:
        """收藏一张图的公共路径：按内容摘要去重 → 落盘 → 描述 → 补向量 → 落三份 JSON。"""
        digest = hashlib.sha1(data).hexdigest()[:12]
        rows = await self.items()
        # 同一张图换个扩展名再传也算重复：只认摘要，不认文件名
        existing = next((r["file"] for r in rows if Path(r["file"]).stem == digest), None)
        if existing:
            return {"ok": True, "name": Path(existing).name, "note": "这张已经在库里了"}
        rel = f"{digest}{suffix}"
        max_total = max(0, int(self.config.get("max_stickers", 200) or 0))
        if len(rows) >= max_total:
            return {"ok": False, "error": f"表情库已达上限 {max_total} 张，先删一些"}

        dest = await asyncio.to_thread(self.store.write_image, rel, data)
        desc = (desc_hint or await self.describe(dest, session) or _stem_desc(label))[:DESC_MAX].strip()
        vecs = await self.matcher.embed([desc])
        await asyncio.to_thread(self._committed, rel, desc, vecs[0] if vecs else None)
        logger.info("表情入库 %s（%s）", Path(rel).name, clip(desc, 40),
                    extra={"session": session_label(session)})
        return {"ok": True, "name": Path(rel).name, "desc": desc, "note": "已收藏进表情库"}

    def _committed(self, rel: str, desc: str, vec) -> None:
        self.store.set_desc(rel, desc)
        if vec is not None:
            self.store.put_vector(rel, desc, vec)
        self.store.flush()

    async def save(self, args: dict, ctx: dict) -> dict:
        """save_emoji：只收 temp 目录下的图（聊天里落地下来的附件就在那里）。"""
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
        if p.stat().st_size > MAX_IMAGE_BYTES:
            return {"ok": False, "error": f"单张图片不超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB"}
        data = await asyncio.to_thread(p.read_bytes)
        return await self.ingest(data, p.suffix.lower(), p.name,
                                 str(args.get("desc") or "").strip(),
                                 session_label(ctx.get("session_id")))

    # ---------- 面板动作 ----------

    async def import_image(self, name: str, data: bytes, desc: str) -> dict:
        """面板上传：只认图片扩展名与大小上限，其余和 save_emoji 走同一条入库路。"""
        suffix = Path(name or "").suffix.lower()
        if suffix not in IMAGE_EXTS:
            return {"ok": False, "error": f"只支持 {'/'.join(sorted(IMAGE_EXTS))}"}
        if not data:
            return {"ok": False, "error": "文件是空的"}
        if len(data) > MAX_IMAGE_BYTES:
            return {"ok": False, "error": f"单张图片不超过 {MAX_IMAGE_BYTES // 1024 // 1024}MB"}
        return await self.ingest(data, suffix, Path(name).stem, (desc or "").strip(), "web")

    async def set_desc(self, rel: str, text: str) -> dict | None:
        """面板改描述：清空就当回到文件名兜底，改完立刻按新描述重算这一条的向量。"""
        clean = (text or "").strip() or _stem_desc(rel)
        if len(clean) > DESC_MAX:
            return {"ok": False, "error": f"描述最长 {DESC_MAX} 字"}
        row = await asyncio.to_thread(self.store.set_desc, rel, clean)
        if row is None:
            return None
        vecs = await self.matcher.embed([row["desc"]]) if row["desc"] else []
        await asyncio.to_thread(self._committed, rel, row["desc"], vecs[0] if vecs else None)
        hit = self.store.vectors().get(rel)
        return {"ok": True, "file": rel, "desc": row["desc"],
                "has_vec": hit is not None and hit[0] == row["desc"],
                "note": "已改描述" + ("" if vecs else "（没配 embedding，这条只按字面匹配）")}

    async def remove(self, rel: str) -> bool:
        """面板删素材：连图片文件一起删——目录是唯一真相，只删索引行下次扫目录又回来了。"""
        gone = await asyncio.to_thread(self.store.delete, rel)
        if gone:
            await asyncio.to_thread(self.store.flush)
            for sid, recent in self._recent.items():
                if rel in recent:
                    recent.remove(rel)
        return gone

    # ---------- 浏览 ----------

    async def list_items(self, args: dict) -> dict:
        limit = max(1, min(50, int(args.get("limit", 20) or 20)))
        query = str(args.get("query") or "").strip()
        rows = await self.items()
        hits, near = await self.matcher.rank(query, rows, limit)
        out = {"ok": True, "total": len(rows),
               "items": [{"name": Path(r["file"]).name, "desc": r["desc"],
                          **({"score": r["score"]} if query else {})}
                         for r in hits]}
        if query and not hits:
            out["near"] = [{"name": Path(r["file"]).name, "desc": r["desc"],
                            "score": r["score"]} for r in near]
        return out


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
        "系统按语义匹配；库里没有贴近的会返回失败并附上几张相近素材的说法，"
        "那时照着说话就行，别硬凑或反复换词试。"
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


class ListEmoji(_EmojiTool):
    name = "list_emoji"
    config_owner = False
    toggleable = False
    description = (
        "看看表情库里有什么素材。query 留空按入库顺序列出全部（最多 limit 条），"
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


class SaveEmoji(_EmojiTool):
    name = "save_emoji"
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
    return [SendEmoji(rt, lib), SaveEmoji(rt, lib), ListEmoji(rt, lib)]
