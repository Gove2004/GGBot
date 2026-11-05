"""长期记忆：结构化写入 + 向量检索 + 取代判定 + 遗忘扫描。

两类记忆各有各的活法：
- `profile`（人物档案）：关于某个人的当前状态，同一份档案靠"新说法改写旧说法"滚动更新，不按年龄删
- `event`（事件流水）：发生过的事，按时间追加，靠到期/淡出/上限清理

写入不再只是追加：一批新事实先找出库里的相关旧条目（向量近邻 + 同一个人的档案），
整批一次交给 LLM 判「新增 / 改写某条 / 不写 / 顺带作废哪几条」，判不了才退回余弦去重直写。
遗忘扫描（sweep）做四件事：到期删、事件淡出、每人档案与整库的超上限淘汰、近似重复合并。

- 嵌入走云端 OpenAI 兼容接口（POST /embeddings），无本地模型依赖；向量存 SQLite（float32 BLOB）
- 未配置 embedding 时自动降级为关键词检索与纯去重直写，不影响机器人启动
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta

import numpy as np

logger = logging.getLogger("ggbot.memory")

DEDUP_SCORE = 0.95        # 没走取代判定时：余弦到这一步才算同一条
CANDIDATE_SCORE = 0.55    # 取代判定的候选门槛：够得着的旧条目才拿给模型看
CANDIDATE_K = 5           # 每条新事实最多带几张向量近邻进判定
SUBJECT_POOL = 8          # 同一个人的档案最多带几张进判定
FADE_KEEP_IMPORTANCE = 4  # 标到 ★4 以上的事件不淡出（人/模型明确说过重要的留住）
HIT_LONGEN = 3.0          # 被命中过的事件，淡出门槛按这个倍数放宽

# 记忆类别的取值与叫法：写入校验、面板的选项与徽章都以这一份为准，前端不再抄第二遍
KIND_LABELS = {"profile": "人物档案", "event": "事件流水"}
IMPORTANCE_MIN = 1
IMPORTANCE_MAX = 5  # ★ 的个数上限

TS_FMT = "%Y-%m-%d %H:%M:%S"

# 合并（近似重复）阈值：记忆越多越宽松。条数一多，同一件事的不同说法很难再撞上 0.95，
# 固定阈值只会让合并越来越无效；按规模放宽，最低降到 TIDY_MIN，防止误合不相干条目。
TIDY_BASE = 0.95
TIDY_MIN = 0.86
TIDY_PER = 50  # 每多 50 条放宽 0.01


def _tidy_threshold(count: int) -> float:
    return max(TIDY_MIN, TIDY_BASE - (count // TIDY_PER) * 0.01)


def kind_of(value) -> str:
    """类别只认 KIND_LABELS 里的取值，认不出的按 profile——写入侧唯一的兜底。"""
    return value if value in KIND_LABELS else "profile"


def clamp_importance(value) -> int:
    """重要度落到 IMPORTANCE_MIN~IMPORTANCE_MAX：模型、面板、CLI 给的数都先过这一道。"""
    try:
        n = int(value or IMPORTANCE_MIN)
    except (TypeError, ValueError):
        return IMPORTANCE_MIN
    return max(IMPORTANCE_MIN, min(IMPORTANCE_MAX, n))


def bigrams(text: str) -> set[str]:
    s = "".join(ch for ch in text if not ch.isspace())
    if len(s) < 2:
        return {s} if s else set()
    return {s[i : i + 2] for i in range(len(s) - 1)}


def _as_ts(value) -> datetime | None:
    """库里的时间字符串 → datetime；空值或畸形按「没有这个时间」处理。"""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:19], TS_FMT)
    except ValueError:
        return None


def normalize_expires(value) -> str | None:
    """到期时间归一：只给日期按当天最后一秒算，空值归 None（不过期）。

    写入路径（工具/面板/CLI/归档）都过这里，否则 `2026-11-20` 这种只有日期的写法
    `_as_ts` 认不出来，到期会变成"永不到期"。
    """
    s = str(value or "").strip().replace("T", " ")
    if not s:
        return None
    return (s + " 23:59:59")[:19] if len(s) == 10 else s


def is_expired(row: dict, now: datetime | None = None) -> bool:
    """到期作废：约定与近况写进去时就带了截止日期。"""
    exp = _as_ts(row.get("expires_at"))
    return exp is not None and exp < (now or datetime.now())


def quiet_days(row: dict, now: datetime | None = None) -> float:
    """离最后一次"被提起"过了多少天：写入时间与命中时间取较晚的那个。"""
    stamps = [t for t in (_as_ts(row.get("last_hit_at")), _as_ts(row.get("ts"))) if t]
    if not stamps:
        return 0.0
    return max(0.0, ((now or datetime.now()) - max(stamps)).total_seconds() / 86400.0)


def retention(row: dict, half_life: float, now: datetime | None = None) -> float:
    """综合保留分 0~1：重要度打底 + 新鲜度衰减（半衰期 = forget_after_days）+ 命中次数加成。

    超上限淘汰与合并择优都按它排序，面板同一列显示——低分不代表"该删"，只是"要先腾的就是它"。
    """
    imp = clamp_importance(row.get("importance")) / float(IMPORTANCE_MAX)
    fresh = 0.5 ** (quiet_days(row, now) / max(1.0, float(half_life)))
    hits = min(1.0, math.log1p(int(row.get("hit_count") or 0)) / math.log1p(10))
    return round(0.4 * imp + 0.4 * fresh + 0.2 * hits, 4)


class MemoryStore:
    def __init__(self, cfg, storage, llm=None):
        self.cfg = cfg
        self.storage = storage
        self.llm = llm
        emb = cfg.embedding
        self.model = emb["model"]
        self.base_url = emb["base_url"]
        self.batch_size = emb["batch_size"]
        self.enabled = emb["enabled"]
        self.client = None
        # 累计写入条数：够一次 sweep_every_writes 就跑一遍遗忘扫描（只删，不合并）
        self._writes = 0
        if self.enabled:
            from openai import OpenAI

            self.client = OpenAI(
                api_key=emb["api_key"], base_url=self.base_url, timeout=60, max_retries=0
            )
        else:
            logger.info("embedding 未启用，长期记忆使用关键词检索")

    @property
    def m(self) -> dict:
        """遗忘与取代的参数，每次现读（面板改完不用重启）。"""
        return self.cfg.memory

    # ---------- 嵌入 ----------

    def embed(self, text: str) -> np.ndarray:
        """单条文本 → 向量。"""
        return self._request([text])[0]

    def embed_many(self, texts: list[str]) -> list[np.ndarray]:
        out: list[np.ndarray] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._request(texts[i : i + self.batch_size]))
        return out

    def _request(self, texts: list[str]) -> list[np.ndarray]:
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self.client.embeddings.create(model=self.model, input=texts)
                rows = sorted(resp.data, key=lambda d: d.index)
                if not rows:
                    raise RuntimeError("embedding 返回为空")
                return [np.array(r.embedding, dtype=np.float32) for r in rows]
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("embedding 调用失败（第 %d 次）: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"Embedding API 调用失败: {last_err}")

    @staticmethod
    def _score(q: np.ndarray, qn: float, v: np.ndarray) -> float:
        denom = qn * float(np.linalg.norm(v))
        if denom < 1e-9:
            return 0.0
        return float(np.dot(q, v)) / denom

    # ---------- 检索 ----------

    def _scope(self, rows: list[dict], session_id: str | None) -> list[dict]:
        """本会话 + 全局（session_id 为 None 时不筛作用域，面板/调试用）。"""
        if session_id is None:
            return rows
        return [r for r in rows if (r.get("session_id") or None) in (None, session_id)]

    def _keyword_scores(self, query: str, rows: list[dict]) -> list[tuple[float, dict]]:
        q = (query or "").strip().lower()
        if not q:
            return []
        qgrams = bigrams(q)
        qchars = {ch for ch in q if not ch.isspace()}
        scored: list[tuple[float, dict]] = []
        for m in rows:
            text = (m.get("content") or "").lower()
            if not text:
                continue
            overlap = len(qgrams & bigrams(text))
            hits = len(qchars & set(text))
            if not overlap and hits < 2:
                continue
            score = max(
                overlap / max(1, len(qgrams)),
                hits / max(1, len(qchars)) * 0.6,
            )
            scored.append((score, m))
        return scored

    @staticmethod
    def _result(m: dict, score: float) -> dict:
        return {
            "id": m["id"],
            "content": m["content"],
            "kind": m.get("type") or "profile",
            "subject": m.get("subject") or "",
            "importance": m["importance"],
            "session_id": m["session_id"],
            "hit_count": int(m.get("hit_count") or 0),
            "last_hit_at": m.get("last_hit_at"),
            "expires_at": m.get("expires_at"),
            "score": round(score, 4),
            "ts": m["ts"],
        }

    def search(self, query: str, k: int = 5, session_id: str | None = None,
               min_score: float | None = None, touch: bool = False) -> list[dict]:
        """检索记忆：启用 embedding 时按余弦相似度，否则按关键词重合度。

        session_id 给定时只取「全局记忆 + 本会话记忆」；为 None 时检索全部（面板/调试）。
        min_score 只对向量检索生效。touch=True 时把命中的条目记一次命中（聊天侧的强化信号），
        面板检索不传——看一眼不该算"想起来过"。
        """
        rows = [r for r in self.storage.get_all_memories() if not is_expired(r)]
        scope = self._scope(rows, session_id)
        if self.enabled:
            with_vec = [r for r in scope if r.get("embedding")]
            if with_vec:
                q = self.embed(query).astype(np.float32)
                qn = float(np.linalg.norm(q))
                scored = []
                for m in with_vec:
                    score = self._score(q, qn, np.frombuffer(m["embedding"], dtype=np.float32))
                    if min_score is not None and score < min_score:
                        continue
                    scored.append((score, m))
                scored.sort(key=lambda x: -x[0])
                hits = [self._result(m, s) for s, m in scored[:k]]
                if touch:
                    self.storage.touch_memories([h["id"] for h in hits])
                return hits
        scored = sorted(self._keyword_scores(query, scope),
                        key=lambda x: (-x[0], -int(x[1].get("importance") or 0)))
        hits = [self._result(m, s) for s, m in scored[:k]]
        if touch:
            self.storage.touch_memories([h["id"] for h in hits])
        return hits

    def _nearest(self, vec: np.ndarray, kind: str, subject: str | None,
                 session_id: str | None, min_score: float,
                 rows: list[dict] | None = None) -> tuple[dict, float] | None:
        """同类同归属里最相似的一条（写入去重用）。"""
        qn = float(np.linalg.norm(vec))
        best: tuple[dict, float] | None = None
        pool = rows if rows is not None else self.storage.get_all_memories()
        for m in pool:
            if (m.get("type") or "profile") != kind:
                continue
            if subject is not None:
                if (m.get("subject") or None) != subject:
                    continue
            elif (m.get("session_id") or None) != (session_id or None):
                continue
            blob = m.get("embedding")
            if not blob:
                continue
            sim = self._score(vec, qn, np.frombuffer(blob, dtype=np.float32))
            if sim >= min_score and (best is None or sim > best[1]):
                best = (m, sim)
        return best

    # ---------- 写入 ----------

    def add(self, content: str, kind: str = "profile", importance: int = 1,
            session_id: str | None = None, subject: str | None = None,
            expires_at: str | None = None) -> str:
        """直写一条记忆（不做取代判定）：同类同归属里余弦 ≥0.95 视为同一条，改写而不是新增。"""
        from ggbot.core.storage import subject_key

        subject = subject_key(subject)
        content = (content or "").strip()
        expires_at = normalize_expires(expires_at)
        if not self.enabled:
            return self.storage.add_memory(session_id, content, kind, importance, None,
                                          subject, expires_at)
        vec = self.embed(content).astype(np.float32)
        dup = self._nearest(vec, kind, subject, session_id, DEDUP_SCORE)
        if dup is not None:
            m, sim = dup
            self.storage.update_memory(
                m["id"], content=content,
                importance=max(int(m.get("importance") or 1), importance),
                embedding=vec.tobytes(),
            )
            logger.info("记忆去重（相似度 %.3f），改写既有条目 %s", sim, m["id"])
            return m["id"]
        return self.storage.add_memory(session_id, content, kind, importance, vec.tobytes(),
                                       subject, expires_at)

    def _candidates(self, fact: dict, vec, rows: list[dict]) -> list[dict]:
        """一条新事实的相关旧条目：同作用域里的向量近邻 + 同一个人的档案。"""
        scope = self._scope(rows, fact.get("session_id"))
        subject = fact.get("subject")
        same_subject = [r for r in scope
                        if subject and (r.get("subject") or None) == subject][:SUBJECT_POOL]
        if vec is not None:
            qn = float(np.linalg.norm(vec))
            scored = []
            for m in scope:
                if not m.get("embedding"):
                    continue
                sim = self._score(vec, qn, np.frombuffer(m["embedding"], dtype=np.float32))
                if sim >= CANDIDATE_SCORE:
                    scored.append((sim, m))
            scored.sort(key=lambda x: -x[0])
            near = [m for _, m in scored[:CANDIDATE_K]]
        else:
            near = [m for _, m in self._keyword_scores(fact["content"], scope)[:CANDIDATE_K]]
        seen, out = set(), []
        for m in same_subject + near:
            if m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
        return out

    def _reconcile(self, facts: list[dict], pools: list[list[dict]]) -> dict[int, dict] | None:
        """整批一次判定：把新事实与各自看到的旧条目交给 LLM，回来一套处置。"""
        if self.llm is None:
            return None
        olds: dict[int, dict] = {}
        number_of: dict[str, int] = {}
        for pool in pools:
            for m in pool:
                if m["id"] not in number_of:
                    n = len(olds) + 1
                    olds[n] = m
                    number_of[m["id"]] = n
        if not olds:
            return None
        items = [{"i": i + 1, "content": f["content"], "kind": f["kind"],
                  "subject": f.get("subject") or "", "importance": f["importance"]}
                 for i, f in enumerate(facts)]
        old_items = [{"n": n, "content": o["content"], "kind": o.get("type") or "profile",
                      "subject": o.get("subject") or ""} for n, o in sorted(olds.items())]
        try:
            verdicts = self.llm.reconcile_memories(items, old_items)
        except Exception as e:  # noqa: BLE001
            logger.warning("记忆取代判定失败，退回直写: %s", e)
            return None
        out: dict[int, dict] = {}
        for v in verdicts:
            try:
                i = int(v.get("i")) - 1
            except (TypeError, ValueError):
                continue
            if not 0 <= i < len(facts):
                continue
            target = v.get("target")
            retire = []
            for n in v.get("retire") or []:
                try:
                    if int(n) in olds:
                        retire.append(olds[int(n)]["id"])
                except (TypeError, ValueError):
                    continue
            out[i] = {
                "action": str(v.get("action") or "new"),
                "target_id": olds[int(target)]["id"] if target and int(target) in olds else None,
                "content": str(v.get("content") or "").strip(),
                "retire": retire,
            }
        return out

    def commit(self, facts: list[dict], session_id: str | None = None) -> dict:
        """写入一批新记忆，写之前先判取代。facts 每项 {content, kind, subject, importance, expires_at}。

        判定失败（没配 LLM、模型乱答、嵌入挂了）不影响写入：退回直写路径，最多多留一条近似记忆，
        由 sweep 的合并那一步兜住。返回 written/updated/dropped/retired 四个计数。
        """
        from ggbot.core.storage import subject_key

        prepared: list[dict] = []
        for f in facts:
            content = str(f.get("content") or "").strip()
            if not content:
                continue
            kind = kind_of(f.get("kind"))
            expires = normalize_expires(f.get("expires_at"))
            if not expires and f.get("expires_in_days"):
                expires = (datetime.now() + timedelta(days=int(f["expires_in_days"]))).strftime(TS_FMT)
            prepared.append({
                "content": content,
                "kind": kind,
                "subject": subject_key(f.get("subject")),
                "importance": clamp_importance(f.get("importance")),
                "session_id": f.get("session_id") or session_id,
                "expires_at": expires,
            })
        if not prepared:
            return {"written": 0, "updated": 0, "dropped": 0, "retired": 0}

        vecs = None
        if self.enabled:
            try:
                vecs = self.embed_many([f["content"] for f in prepared])
            except Exception as e:  # noqa: BLE001
                logger.warning("新记忆嵌入失败，退回关键词候选: %s", e)
        rows = [r for r in self.storage.get_all_memories() if not is_expired(r)]
        pools = [self._candidates(f, vecs[i] if vecs else None, rows)
                 for i, f in enumerate(prepared)]
        verdicts = None
        if any(pools) and self.m["reconcile"]:
            verdicts = self._reconcile(prepared, pools)

        written = updated = dropped = 0
        retire_ids: list[str] = []
        for i, f in enumerate(prepared):
            v = (verdicts or {}).get(i) or {}
            action = v.get("action") or "new"
            retire_ids.extend(v.get("retire") or [])
            target_id = v.get("target_id")
            content = v.get("content") or f["content"]
            if action == "drop":
                dropped += 1
                continue
            if action == "update" and target_id:
                self.update(target_id, content, f["importance"])
                updated += 1
                continue
            vec = vecs[i] if vecs else None
            if vec is not None:
                dup = self._nearest(vec.astype(np.float32), f["kind"], f["subject"],
                                   f["session_id"], DEDUP_SCORE, rows)
                if dup is not None:
                    self.update(dup[0]["id"], content, f["importance"])
                    updated += 1
                    continue
            self.storage.add_memory(f["session_id"], content, f["kind"], f["importance"],
                                    vec.astype(np.float32).tobytes() if vec is not None else None,
                                    f["subject"], f["expires_at"])
            written += 1

        if retire_ids:
            self.storage.delete_memories(sorted(set(retire_ids)))
            logger.info("记忆取代：作废被推翻的旧条目 %d 条", len(set(retire_ids)))
        self._writes += written + updated
        if self._writes >= self.m["sweep_every_writes"]:
            self._writes = 0
            self.sweep(auto=True)
        return {"written": written, "updated": updated, "dropped": dropped,
                "retired": len(set(retire_ids))}

    def update(self, memory_id: str, content: str | None = None, importance: int | None = None,
               subject: str | None = None, kind: str | None = None,
               expires_at: str | None = None) -> bool:
        """编辑记忆（改内容会重新向量化），条目不存在返回 False。

        expires_at 传空串表示"取消到期"；subject 传空串表示"不归属于某个人"。
        """
        if not self.storage.get_memory(memory_id):
            return False
        vec = None
        if content is not None:
            content = content.strip()
            if not content:
                raise ValueError("记忆内容不能为空")
            # 未启用 embedding 时清空旧向量，避免内容与向量不一致
            vec = self.embed(content).astype(np.float32) if self.enabled else b""
        # None = 不动到期时间；给了值就归一，清空要保留成假值（""）传给存储层
        if expires_at is not None:
            expires_at = normalize_expires(expires_at) or ""
        self.storage.update_memory(
            memory_id,
            content=content,
            importance=importance,
            embedding=vec.tobytes() if isinstance(vec, np.ndarray) else vec,
            subject=subject,
            type_=kind,
            expires_at=expires_at,
        )
        return True

    # ---------- 遗忘 ----------

    def _to_delete(self, rows: list[dict], cfg: dict, now: datetime) -> tuple[list[str], list[str], list[str]]:
        """该删的三批：到期、事件淡出、超上限（每人档案 + 整库）。"""
        forget = cfg["forget_after_days"]
        expired, faded = [], []
        for r in rows:
            if is_expired(r, now):
                expired.append(r["id"])
            elif (r.get("type") or "profile") == "event" \
                    and int(r.get("importance") or 1) < FADE_KEEP_IMPORTANCE:
                line = forget * (HIT_LONGEN if int(r.get("hit_count") or 0) > 0 else 1)
                if quiet_days(r, now) > line:
                    faded.append(r["id"])
        gone = set(expired) | set(faded)
        alive = [r for r in rows if r["id"] not in gone]

        over: list[str] = []
        groups: dict[str, list[dict]] = {}
        for r in alive:
            subject = r.get("subject") or ""
            if (r.get("type") or "profile") == "profile" and subject:
                groups.setdefault(subject, []).append(r)
        for subject, group in groups.items():
            if len(group) > cfg["max_per_subject"]:
                group.sort(key=lambda r: (retention(r, forget, now), r.get("ts") or ""))
                over.extend(r["id"] for r in group[: len(group) - cfg["max_per_subject"]])
        alive = [r for r in alive if r["id"] not in set(over)]
        if len(alive) > cfg["max_memories"]:
            alive.sort(key=lambda r: (retention(r, forget, now), r.get("ts") or ""))
            over.extend(r["id"] for r in alive[: len(alive) - cfg["max_memories"]])
        return expired, faded, over

    def _merge(self, threshold: float | None, rows: list[dict], cfg: dict,
               now: datetime) -> list[str]:
        """近似重复合并：余弦过阈值的算同一条，留保留分高的那条（同分留新的）。"""
        if not self.enabled:
            return []
        vec_rows = [r for r in rows if r.get("embedding")]
        if len(vec_rows) < 2:
            return []
        thr = threshold if threshold is not None else _tidy_threshold(len(rows))
        mat = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in vec_rows])
        norms = np.linalg.norm(mat, axis=1)
        mat = mat / np.where(norms < 1e-9, 1.0, norms)[:, None]
        dropped: set[int] = set()
        for i in range(len(vec_rows)):
            if i in dropped:
                continue
            sims = mat[i + 1 :] @ mat[i]
            for offset in np.flatnonzero(sims >= thr):
                j = int(offset) + i + 1
                if j in dropped:
                    continue
                a, b = vec_rows[i], vec_rows[j]
                keep_a = (retention(a, cfg["forget_after_days"], now), a.get("ts") or "") >= \
                    (retention(b, cfg["forget_after_days"], now), b.get("ts") or "")
                dropped.add(j if keep_a else i)
                if not keep_a:
                    break
        return [vec_rows[i]["id"] for i in sorted(dropped)]

    def sweep(self, threshold: float | None = None, auto: bool = False) -> dict:
        """遗忘扫描：到期 → 事件淡出 → 每人档案与整库超上限 → 近似重复合并。

        auto=True（写入攒够时顺手跑）只做前三步：那些都是纯本地筛选；合并要过一遍全库向量，
        只在启动、面板「立即整理」和 CLI 这些后台场合做，免得拖慢对话回合。
        """
        cfg = self.m
        now = datetime.now()
        rows = self.storage.get_all_memories()
        expired, faded, over = self._to_delete(rows, cfg, now)
        doomed = set(expired) | set(faded) | set(over)
        merged: list[str] = []
        if not auto:
            merged = [m for m in self._merge(threshold, [r for r in rows if r["id"] not in doomed],
                                             cfg, now) if m not in doomed]
        removed = list(doomed | set(merged))
        if removed:
            self.storage.delete_memories(removed)
            logger.info("记忆遗忘 到期 %d · 淡出 %d · 超限 %d · 合并 %d（共 %d 条）",
                        len(expired), len(faded), len(over), len(merged),
                        len(self.storage.get_all_memories()))
        return {"expired": len(expired), "faded": len(faded), "over_cap": len(over),
                "merged": len(merged), "total": len(rows) - len(removed)}

    def usage(self) -> dict:
        """记忆页的容量读数：口径与 `_to_delete` 同一条，只是不动数据。

        `countable` 才是真正占整库上限的条数——到期和淡出的条目已经注定要腾走，
        把它们算进「离上限还有多远」会让提示在扫描前一秒变成假警报。"""
        cfg = self.m
        now = datetime.now()
        rows = self.storage.capacity_rows()
        expired, faded, over = self._to_delete(rows, cfg, now)
        doomed = set(expired) | set(faded) | set(over)
        groups: dict[str, int] = {}
        for r in rows:
            if r["id"] in doomed:
                continue
            if (r.get("type") or "profile") == "profile" and r.get("subject"):
                groups[r["subject"]] = groups.get(r["subject"], 0) + 1
        top_subject, top_count = max(groups.items(), key=lambda kv: (kv[1], kv[0]), default=("", 0))
        return {
            "stored": len(rows),
            "expired": len(expired),
            "faded": len(faded),
            "countable": len(rows) - len(expired) - len(faded),
            "over_cap": len(over),
            "max_memories": cfg["max_memories"],
            "top_subject": top_subject,
            "top_count": top_count,
            "max_per_subject": cfg["max_per_subject"],
        }

    def panel_rows(self, rows: list[dict] | None = None) -> list[dict]:
        """面板与 CLI 用的带分数版本：每条补保留分与剩余天数。"""
        cfg = self.m
        now = datetime.now()
        out = []
        for r in rows if rows is not None else self.storage.list_memories(None, 500):
            d = dict(r)
            d["kind"] = d.get("kind") or d.get("type") or "profile"
            d["retention"] = retention(d, cfg["forget_after_days"], now)
            d["quiet_days"] = round(quiet_days(d, now), 1)
            exp = _as_ts(d.get("expires_at"))
            d["expired"] = exp is not None and exp < now
            out.append(d)
        return out

    def embed_status(self) -> dict:
        return {
            "enabled": self.enabled,
            "provider": self.cfg.embedding.get("provider", ""),
            "model": self.model,
            "total": len(self.storage.get_all_memories()),
        }
