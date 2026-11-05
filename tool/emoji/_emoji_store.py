"""表情库的文件系统那一层：素材目录 + 三份 JSON，谁变过就只重写谁。

目录是唯一真相：库里的图片就是素材（旧机器的 `stickers/`、`user/` 子目录递归扫进来，仍算同一个库）。
- `index.json` v4：`{file, desc, added_at}` —— 人看的素材清单，可手改、入库
- `vectors.json`：`{file: {desc, vec}}` —— 派生缓存，不入库；描述改了旧向量作废
- `usage.json`：`{file: {use_count, last_used_at}}` —— 派生缓存，供挑选轮动与面板展示

三份分开读写是刻意的：三千张素材的向量整包十几 MB，「浏览一次就重扫+整包重写」必然卡；
所以这里只给出只读视图 `rows()`，写入全部走显式动作（补索引、算向量、改描述、删图、记一次使用），
`flush()` 只落真正变过的那几份。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from ggbot.util.media import EMOJI_IMAGE_EXTS

logger = logging.getLogger("ggbot.tools.emoji")

# 表情素材的扩展名只有一份口径（ggbot.util.media），本地媒体白名单与这里同进同退
IMAGE_EXTS = EMOJI_IMAGE_EXTS
DATA_KEY = "emoji"
DESC_MAX = 200           # 一句话描述的长度上限（面板也按这个数拦）
MAX_IMAGE_BYTES = 8 * 1024 * 1024   # 单张素材封顶：QQ 本地直传也吃不下更大的
SCAN_TTL_SEC = 3.0       # 扫目录结果的复用窗口：一次回合里 send/list 不该各扫一遍
EMBED_BATCH = 64         # 一次嵌入请求带几条描述：整库首扫时几千条一起发会被厂商拒

DEFAULT_CONFIG = {
    "enabled": True,
    "max_per_turn": 1,
    "min_interval_sec": 10,
    "max_stickers": 200,
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


def _safe_rel(rel: str) -> bool:
    return bool(rel) and ".." not in Path(rel).parts and not rel.startswith(("/", "\\")) \
        and "\x00" not in rel


def _file_version(data: dict | None) -> int:
    """索引文件里的格式版本；文件不存在按「已是最新、没东西要升级」算。"""
    if data is None:
        return 4
    try:
        return int(data.get("version") or 0)
    except (TypeError, ValueError):
        return 0


class EmojiStore:
    """素材目录与其三份 JSON 的唯一读写口；不做检索，也不发送。"""

    def __init__(self, rt):
        self.rt = rt
        self._index: tuple[int, dict] | None = None      # (文件 mtime, 解析结果)
        self._vectors: tuple[int, dict] | None = None
        self._usage: tuple[int, dict] | None = None
        self._files: tuple[float, list[str], int] = (0.0, [], 0)  # (扫的时刻, 相对路径, 目录修改时刻)
        self._dirty: set[str] = set()
        self._index_version = 4  # 读到旧格式时记下它，让写入动作顺手升级回来

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
    def usage_path(self) -> Path:
        return self.root / "usage.json"

    @property
    def config(self) -> dict:
        return self.rt.load_config(DATA_KEY, DEFAULT_CONFIG)

    def image_path(self, rel: str) -> Path | None:
        """把 Web 传来的相对路径解析成素材文件；不在库里、不是图片或已被删掉都返回 None。"""
        if not _safe_rel(rel):
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

    # ---------- 三份 JSON（各自按 mtime 复用解析结果） ----------

    @staticmethod
    def _stamp(p: Path) -> int:
        try:
            return p.stat().st_mtime_ns
        except OSError:
            return -1

    @staticmethod
    def _read_json(p: Path) -> dict | None:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            logger.warning("表情库 %s 读取失败，按空处理: %s", p.name, e)
            return None
        return data if isinstance(data, dict) else None

    def index(self) -> dict[str, dict]:
        """{相对路径: {file, desc, added_at}}——手改过的字段也照单收下。"""
        stamp = self._stamp(self.index_path)
        if self._index and self._index[0] == stamp:
            return self._index[1]
        data = self._read_json(self.index_path) if stamp > 0 else None
        self._index_version = _file_version(data)
        rows: dict[str, dict] = {}
        raw_rows = (data or {}).get("items", [])
        for raw in raw_rows if isinstance(raw_rows, list) else []:
            if not isinstance(raw, dict):
                continue
            rel = str(raw.get("file") or "")
            if not _safe_rel(rel):
                continue  # 手改出来的越界路径一律不认
            desc = str(raw.get("desc") or "")
            if not desc:
                # v2 索引只有 moods 词表：拼成一句话当描述，向量重算
                desc = " ".join(str(m) for m in (raw.get("moods") or []) if m)
            rows[rel] = {"file": rel, "desc": desc[:DESC_MAX].strip(),
                         "added_at": str(raw.get("added_at") or "")}
        self._index = (stamp, rows)
        return rows

    def vectors(self) -> dict[str, tuple[str, np.ndarray]]:
        """向量缓存：{文件: (生成时的描述, 向量)}——描述改了，旧向量就作废。"""
        stamp = self._stamp(self.vectors_path)
        if self._vectors and self._vectors[0] == stamp:
            return self._vectors[1]
        data = self._read_json(self.vectors_path) if stamp > 0 else None
        rows = (data or {}).get("vectors", data) if data else None
        out: dict[str, tuple[str, np.ndarray]] = {}
        for rel, raw in (rows.items() if isinstance(rows, dict) else []):
            if not isinstance(raw, dict):
                continue
            vec = _decode_vec(raw.get("vec"))
            if vec is not None and _safe_rel(str(rel)):
                out[str(rel)] = (str(raw.get("desc") or ""), vec)
        self._vectors = (stamp, out)
        return out

    def usage(self) -> dict[str, dict]:
        """{文件: {use_count, last_used_at}}——发出去一次记一笔，重启也不丢。"""
        stamp = self._stamp(self.usage_path)
        if self._usage and self._usage[0] == stamp:
            return self._usage[1]
        data = self._read_json(self.usage_path) if stamp > 0 else None
        rows = (data or {}).get("usage", data) if data else None
        out: dict[str, dict] = {}
        for rel, raw in (rows.items() if isinstance(rows, dict) else []):
            if not isinstance(raw, dict) or not _safe_rel(str(rel)):
                continue
            try:
                count = int(raw.get("use_count") or 0)
            except (TypeError, ValueError):
                count = 0
            out[str(rel)] = {"use_count": max(0, count),
                             "last_used_at": str(raw.get("last_used_at") or "")}
        self._usage = (stamp, out)
        return out

    # ---------- 扫目录 ----------

    def files(self, force: bool = False) -> list[str]:
        """库里的图片（相对路径）：目录里有什么就是什么，一个统一库。

        结果按 `SCAN_TTL_SEC` 复用——回合内 send/list 各扫一次三千张的目录纯属浪费；
        但库目录本身一变（丢图/删图）立刻重扫，不等这 3 秒；面板的「刷新」和任何写入动作
        会 force/失效它，所以手动放进目录的图下一次就能挑到。
        """
        now = time.monotonic()
        stamp = self._dir_stamp()
        if not force and stamp == self._files[2] and now - self._files[0] < SCAN_TTL_SEC:
            return self._files[1]
        root = self.root
        found: list[str] = []
        stack = [root]
        while stack:
            d = stack.pop()
            try:
                entries = sorted(os.scandir(d), key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if e.name.startswith("."):
                    continue
                if e.is_dir():
                    stack.append(Path(e.path))
                elif os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                    found.append(Path(e.path).relative_to(root).as_posix())
        found.sort()
        self._files = (now, found, stamp)
        return found

    def _dir_stamp(self) -> int:
        """库目录的修改时刻：往里丢一张图它就变，比整目录扫一遍便宜得多。"""
        try:
            return self.root.stat().st_mtime_ns
        except OSError:
            return 0

    def _rescan(self) -> None:
        self._files = (0.0, [], 0)

    # ---------- 只读视图 ----------

    def rows(self, force: bool = False) -> list[dict]:
        """目录 + 索引 + 向量 + 使用统计拼出的当前视图：只读，不落盘、不嵌入。

        目录里多出来的图照样出现在这里（描述用文件名兜底），只是还没进索引——
        下一次聊天（`list_emoji` / `send_emoji`）或面板上改一次描述才把它补进去。
        """
        indexed = self.index()
        vecs = self.vectors()
        usage = self.usage()
        out = []
        for rel in self.files(force):
            row = indexed.get(rel)
            if row is None:
                row = {"file": rel, "desc": _stem_desc(rel), "added_at": _now()}
            hit = vecs.get(rel)
            use = usage.get(rel) or {}
            out.append({
                "file": rel,
                "desc": row["desc"],
                "added_at": row["added_at"],
                "vec": hit[1] if hit is not None and hit[0] == row["desc"] else None,
                "use_count": int(use.get("use_count") or 0),
                "last_used_at": str(use.get("last_used_at") or ""),
            })
        return out

    # ---------- 写入 ----------

    def ensure(self, rows: list[dict]) -> int:
        """让索引跟上目录：新图补一行，删掉的图把三份记录里的残留一起清掉，老格式顺手升级成 v4。"""
        idx = self.index()
        alive = set()
        changed = 0
        for r in rows:
            alive.add(r["file"])
            if r["file"] not in idx:
                idx[r["file"]] = {"file": r["file"], "desc": r["desc"],
                                  "added_at": r["added_at"]}
                changed += 1
        vecs, usage = self.vectors(), self.usage()
        for rel in [*idx]:
            if rel not in alive:
                idx.pop(rel)
                if vecs.pop(rel, None) is not None:
                    self._dirty.add("vectors")
                if usage.pop(rel, None) is not None:
                    self._dirty.add("usage")
                changed += 1
        if changed or self._index_version != 4:
            self._dirty.add("index")
        return changed

    def put_vector(self, rel: str, desc: str, vec: np.ndarray) -> None:
        self.vectors()[rel] = (desc, vec)
        self._dirty.add("vectors")

    def drop_vector(self, rel: str) -> None:
        if self.vectors().pop(rel, None) is not None:
            self._dirty.add("vectors")

    def set_desc(self, rel: str, text: str) -> dict | None:
        """改一条描述（图在目录里但索引没有就先补一行）。描述变了，旧向量当场作废。"""
        if rel not in set(self.files()):
            return None
        idx = self.index()
        row = idx.get(rel)
        if row is None:
            row = idx[rel] = {"file": rel, "desc": "", "added_at": _now()}
        clean = text[:DESC_MAX].strip()
        if row["desc"] != clean:
            row["desc"] = clean
            self._dirty.add("index")
            if self.vectors().pop(rel, None) is not None:
                self._dirty.add("vectors")
        return {**row, "vec": (self.vectors().get(rel) or (None, None))[1]}

    def write_image(self, rel: str, data: bytes) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self._rescan()
        return p

    def delete(self, rel: str) -> bool:
        """连图片文件一起删：目录是真相，只删索引行明天扫目录它又回来了。"""
        if rel not in set(self.files(force=True)):
            return False
        try:
            (self.root / rel).unlink(missing_ok=True)
        except OSError as e:
            logger.warning("表情素材删除失败 %s: %s", rel, e)
            return False
        for attr, dirty in (("_index", "index"), ("_vectors", "vectors"), ("_usage", "usage")):
            hit = getattr(self, attr)
            if hit and hit[1].pop(rel, None) is not None:
                self._dirty.add(dirty)
        self._rescan()
        return True

    def record_use(self, rel: str) -> None:
        row = self.usage().get(rel) or {"use_count": 0, "last_used_at": ""}
        row["use_count"] = int(row["use_count"]) + 1
        row["last_used_at"] = _now()
        self.usage()[rel] = row
        self._dirty.add("usage")

    def flush(self) -> None:
        """只重写变过的那几份；写完把缓存时间戳推到新值，免得自己再解析一遍十几 MB。"""
        if "index" in self._dirty:
            items = [{"file": r, **self.index()[r]} for r in sorted(self.index())]
            self._write_json(self.index_path, {"version": 4, "items": items}, "_index")
            self._index_version = 4
        if "vectors" in self._dirty:
            vecs = self.vectors()
            payload = {"vectors": {rel: {"desc": desc, "vec": _encode_vec(v)}
                                   for rel, (desc, v) in sorted(vecs.items())}}
            self._write_json(self.vectors_path, payload, "_vectors")
        if "usage" in self._dirty:
            payload = {"usage": {rel: dict(v) for rel, v in sorted(self.usage().items())}}
            self._write_json(self.usage_path, payload, "_usage")
        self._dirty.clear()

    def _write_json(self, path: Path, payload: dict, cache: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
        tmp.replace(path)
        hit = getattr(self, cache)
        if hit is not None:
            setattr(self, cache, (self._stamp(path), hit[1]))
