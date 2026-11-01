"""SQLite 持久化层：会话、消息、被动回复锚点、发送回执、记忆、提醒。

全部落盘，重启不丢。线程安全：每个操作一个连接（用完即关）+ WAL，读写都在 self._lock 里串行。
表结构只有一份（`SCHEMA`）：v3 起不再就地迁移旧库，旧数据要么用 `--reset-db` 重建，要么留着别开。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    sender TEXT,
    sender_name TEXT,
    content TEXT,
    attachments TEXT,
    msg_id TEXT,
    msg_seq INTEGER,
    event_id TEXT,
    is_at INTEGER DEFAULT 0,
    passive_ok INTEGER DEFAULT 0,
    read_state TEXT NOT NULL DEFAULT 'unread',
    reply_state TEXT NOT NULL DEFAULT 'none',
    ts TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE,
    scene TEXT,
    archived_upto INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reply_anchors(
    session_id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    event_id TEXT,
    scene TEXT,
    next_seq INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,
    updated_at TEXT,
    PRIMARY KEY(session_id, msg_id)
);
CREATE TABLE IF NOT EXISTS sends(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    msg_id TEXT,
    msg_seq INTEGER,
    via TEXT,
    kind TEXT,
    ok INTEGER NOT NULL DEFAULT 1,
    detail TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS memories(
    id TEXT PRIMARY KEY,
    session_id TEXT,
    type TEXT,
    content TEXT,
    embedding BLOB,
    importance INTEGER DEFAULT 1,
    ts TEXT,
    subject TEXT,
    hit_count INTEGER DEFAULT 0,
    last_hit_at TEXT,
    expires_at TEXT
);
CREATE TABLE IF NOT EXISTS actors(
    openid TEXT PRIMARY KEY,
    name TEXT,
    level INTEGER NOT NULL DEFAULT 2,
    note TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    owner TEXT,
    owner_name TEXT,
    notify TEXT,
    notify_name TEXT,
    due_ts INTEGER NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT,
    fired_at TEXT,
    rule TEXT NOT NULL DEFAULT '',
    times INTEGER NOT NULL DEFAULT 1,
    fired_count INTEGER NOT NULL DEFAULT 0,
    last_fired_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_msg_id ON messages(msg_id);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_ts);
CREATE INDEX IF NOT EXISTS idx_reminders_session ON reminders(session_id, status);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 一条消息的两条独立时间线：
#   已读 read_state —— 有没有被读进上下文；
#   已回 reply_state —— 这条要不要回、回合处理它的结果。
# 「到达」不占列：行存在 + ts 就是到达，角色（user/assistant）就是方向。
READ_UNREAD = "unread"
READ_READ = "read"

REPLY_BACKLOG = "waiting"    # 待处理：等上线窗口或回合消费
REPLY_ANSWERED = "answered"  # 已回
REPLY_FAILED = "failed"      # 这一回合尝试回复但失败
REPLY_SKIPPED = "skipped"    # 这一回合并未回复（silence / 模型无话可说）
REPLY_NONE = "none"          # 不需要回复：助手自己的消息、只读进上下文的积压

REPLY_STATES = (REPLY_BACKLOG, REPLY_ANSWERED, REPLY_FAILED, REPLY_SKIPPED, REPLY_NONE)


REMINDER_STATUS = ("pending", "fired", "cancelled", "expired")
"""pending = 还在排（周期项发完下一场也还是 pending）；其余三种是终态，只被清理退场。"""


def subject_key(name: str | None) -> str | None:
    """记忆归属的人名规范化：折叠空白、掐到 40 字，空串归成 None。

    「同一个人的档案」靠这个名字聚在一起，所以写法上的空格与全角差异要抹平；
    不做同义合并（"小明"和"明哥"是两条），那是模型的判断，不是字符串能判的。
    """
    s = " ".join(str(name or "").split())
    return s[:40] or None


def _reminder_filter(session_id: str | None, status: str | None,
                     q: str | None) -> tuple[str, tuple]:
    """日程列表/计数的共用条件（面板、CLI、批量清理同一份口径）。"""
    clauses: list[str] = []
    params: list = []
    if session_id:
        clauses.append("session_id=?")
        params.append(session_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    needle = (q or "").strip()
    if needle:
        # 关键词是"像什么"，不是"匹配模式"：% _ 得当字面量，否则搜 % 等于全命中
        like = "%" + needle.replace("%", "\\%").replace("_", "\\_") + "%"
        clauses.append("(text LIKE ? ESCAPE '\\' OR COALESCE(notify_name,'') LIKE ? ESCAPE '\\'"
                       " OR COALESCE(owner_name,'') LIKE ? ESCAPE '\\')")
        params += [like, like, like]
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


def _memory_filter(session_id: str | None, subject: str | None, kind: str | None) -> tuple[str, tuple]:
    """记忆查询/删除的共用条件片段（面板、CLI、批量清理都走这一份）。"""
    clauses: list[str] = []
    params: list = []
    if session_id:
        clauses.append("session_id=?")
        params.append(session_id)
    if kind:
        clauses.append("type=?")
        params.append(kind)
    if subject is not None:
        key = subject_key(subject)
        if key:
            clauses.append("subject=?")
            params.append(key)
        else:
            clauses.append("COALESCE(subject,'')=''")
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), tuple(params)


def _row_to_dict(row) -> dict:
    """sqlite 行转 dict，并反序列化 attachments（入库时 json.dumps 存储）。"""
    d = dict(row)
    if isinstance(d.get("attachments"), str):
        try:
            d["attachments"] = json.loads(d["attachments"])
        except (json.JSONDecodeError, TypeError):
            d["attachments"] = []
    if "is_at" in d:
        d["is_at"] = bool(d["is_at"])
    if "passive_ok" in d:
        d["passive_ok"] = bool(d["passive_ok"])
    return d


class Storage:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """一次操作一个连接：正常退出提交，异常时未提交的内容随 close 丢弃。

        sqlite3 的连接当上下文管理器只管提交、不管关闭，所以必须自己兜住 close：
        连接不关就等于每次调用泄漏一对文件描述符（ggbot.db 与 ggbot.db-wal），
        进程 soft nofile 顶满后 Web accept 报 Errno 24，asyncio 失败即重试把 CPU 吃满。
        """
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
            conn.commit()
        finally:
            conn.close()

    def close(self) -> None:
        """收尾（进程退出、测试清理前调用）：把 WAL 归并回主库，-wal/-shm 随之清空。

        连接随每次操作关闭，这里没有长驻连接要释放。
        """
        with self._lock, self._conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ---------- 会话 ----------

    def get_or_create_session(self, key: str, scene: str = "group") -> str:
        """按 key（如 group:{gid} / c2c:{openid}）取或建会话；**session id 直接等于 key**，
        保留场景前缀（group:/c2c:），供展示层区分群聊/私聊，且天然按会话隔离。
        旧库（uuid 哈希 id）数据视为已污染：清空 messages/sessions 后重建（见 reset_chat_data）。"""
        with self._lock, self._conn() as c:
            row = c.execute("SELECT id FROM sessions WHERE key=?", (key,)).fetchone()
            if row:
                c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (_now(), row["id"]))
                return row["id"]
            c.execute(
                "INSERT INTO sessions(id, key, scene, created_at, updated_at) VALUES(?,?,?,?,?)",
                (key, key, scene, _now(), _now()),
            )
            return key

    def session_target(self, session_id: str) -> tuple[str, str] | None:
        """反查会话的发送目标与场景。

        session_id 即 sessions.key（group:<openid> / c2c:<openid>），
        这里统一从库里取，兼容历史数据。
        """
        with self._lock, self._conn() as c:
            row = c.execute("SELECT key FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            return None
        key = (row["key"] or "").strip()
        if ":" in key:
            prefix, _, rest = key.partition(":")
            return rest, ("group" if prefix == "group" else "c2c")
        return (key or None, "group")

    def get_archive_cursor(self, session_id: str) -> int:
        """已归档到的消息 id（该 id 及更早的历史已进入长期记忆）。"""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT archived_upto FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return int(row["archived_upto"] or 0) if row else 0

    def set_archive_cursor(self, session_id: str, message_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE sessions SET archived_upto=? WHERE id=?", (int(message_id), session_id)
            )

    def get_archive_batch(
        self, session_id: str, after_id: int, before_id: int, limit: int
    ) -> list[dict]:
        """待归档的历史消息：id 在 (after_id, before_id) 之间，按时间正序。
        after_id 是归档游标，before_id 是本轮留在窗口里的第一条（不进这一批）。"""
        if limit <= 0 or before_id <= after_id:
            return []
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? AND id>? AND id<? "
                "ORDER BY id ASC LIMIT ?",
                (session_id, int(after_id), int(before_id), int(limit)),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_sessions(self, limit: int = 200) -> list[dict]:
        """会话列表（面板/CLI）：带消息数、待处理数、最后一条消息。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"""
                SELECT s.id, s.key, s.scene, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id
                          AND m.reply_state='{REPLY_BACKLOG}') AS waiting_count,
                       (SELECT MAX(m.ts) FROM messages m WHERE m.session_id=s.id) AS last_ts,
                       (SELECT m.content FROM messages m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) AS last_message,
                       (SELECT m.role FROM messages m WHERE m.session_id=s.id ORDER BY m.id DESC LIMIT 1) AS last_role
                FROM sessions s
                ORDER BY COALESCE((SELECT MAX(m.ts) FROM messages m WHERE m.session_id=s.id), s.updated_at) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_session(self, session_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                f"""
                SELECT s.id, s.key, s.scene, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id
                          AND m.reply_state='{REPLY_BACKLOG}') AS waiting_count,
                       (SELECT MAX(m.ts) FROM messages m WHERE m.session_id=s.id) AS last_ts
                FROM sessions s WHERE s.id=?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def clear_session_context(self, session_id: str) -> dict:
        """清空单个会话的上下文（消息、归档游标与出站记账），保留会话本身与记忆。"""
        with self._lock, self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
            c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            # 锚点与发送回执是这批消息的账：消息没了还留着，面板会显示「消息已清空、出站记录还在」
            c.execute("DELETE FROM reply_anchors WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM sends WHERE session_id=?", (session_id,))
            c.execute(
                "UPDATE sessions SET archived_upto=0, updated_at=? WHERE id=?",
                (_now(), session_id),
            )
        return {"messages": n}

    # ---------- 消息 ----------

    def add_message(
        self,
        session_id: str,
        role: str,
        sender: str,
        content: str,
        attachments: list | None = None,
        msg_id: str | None = None,
        msg_seq: int | None = None,
        event_id: str | None = None,
        is_at: bool = False,
        passive_ok: bool | None = None,
        read_state: str | None = None,
        reply_state: str | None = None,
        sender_name: str | None = None,
    ) -> int:
        if passive_ok is None:
            passive_ok = is_at
        # 两条线各归各：助手的话是自己说的（已读、不需要回复），用户的话进来就是未读 + 待处理
        if read_state is None:
            read_state = READ_READ if role == "assistant" else READ_UNREAD
        if reply_state is None:
            reply_state = REPLY_NONE if role == "assistant" else REPLY_BACKLOG
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages(session_id, role, sender, sender_name, content, attachments, "
                "msg_id, msg_seq, event_id, is_at, passive_ok, read_state, reply_state, ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    role,
                    sender,
                    sender_name,
                    content,
                    json.dumps(attachments or [], ensure_ascii=False),
                    msg_id,
                    msg_seq,
                    event_id,
                    1 if is_at else 0,
                    1 if passive_ok else 0,
                    read_state,
                    reply_state,
                    _now(),
                ),
            )
            return cur.lastrowid

    def message_exists(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        with self._lock, self._conn() as c:
            return c.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone() is not None

    def mark_message_at(self, msg_id: str, event_id: str | None = None) -> bool:
        """给已入库的那行补上 @ 标记，返回是否真的改过（没改过 = 本来就是 @ 或行不在）。

        同一条群消息平台会推两次：全量事件先到时未必认出是被@本机器人，随后的 @ 事件才确定。
        只挪标记、不插第二条，正文与状态都不动。
        """
        if not msg_id:
            return False
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE messages SET is_at=1, passive_ok=1, "
                "event_id=CASE WHEN ?<>'' AND (event_id IS NULL OR event_id='') THEN ? ELSE event_id END "
                "WHERE msg_id=? AND role='user' AND is_at=0",
                (event_id or "", event_id, msg_id),
            )
            return cur.rowcount > 0

    def get_recent_messages(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def latest_replyable_message(self, session_id: str, since: str) -> dict | None:
        """本会话窗口内最近一条可被动回复的用户消息（被动回复锚点）。

        ts 是 "YYYY-MM-DD HH:MM:SS"，字典序即时间序，所以过期判断直接拿字符串比。
        """
        if not session_id:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT msg_id, event_id, ts FROM messages WHERE session_id=? AND role='user' "
                "AND passive_ok=1 AND msg_id IS NOT NULL AND msg_id<>'' AND ts>=? "
                "ORDER BY id DESC LIMIT 1",
                (session_id, since),
            ).fetchone()
        return dict(row) if row else None

    def find_message_id(self, session_id: str, needle: str) -> str | None:
        """按正文关键词找本会话最近一条有 msg_id 的消息（用于引用回复）。"""
        needle = (needle or "").strip()
        if not session_id or not needle:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT msg_id FROM messages WHERE session_id=? AND instr(content, ?) > 0 "
                "AND msg_id IS NOT NULL AND msg_id<>'' ORDER BY id DESC LIMIT 1",
                (session_id, needle),
            ).fetchone()
        return row["msg_id"] if row else None

    def find_sender_openid(self, name: str, session_id: str | None = None) -> str | None:
        """按昵称反查 openid：先找本会话发言者，再找显式权限成员。"""
        name = (name or "").strip().lstrip("@")
        if not name:
            return None
        with self._lock, self._conn() as c:
            if session_id:
                row = c.execute(
                    "SELECT sender FROM messages WHERE session_id=? AND role='user' "
                    "AND sender_name=? AND sender IS NOT NULL AND sender<>'' "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, name),
                ).fetchone()
                if row:
                    return row["sender"]
            row = c.execute(
                "SELECT sender FROM messages WHERE role='user' AND sender_name=? "
                "AND sender IS NOT NULL AND sender<>'' ORDER BY id DESC LIMIT 1",
                (name,),
            ).fetchone()
            if row:
                return row["sender"]
            row = c.execute(
                "SELECT openid FROM actors WHERE name=? ORDER BY level DESC, updated_at DESC LIMIT 1",
                (name,),
            ).fetchone()
        return row["openid"] if row else None

    def get_backlog_messages(self, session_id: str, limit: int = 1000) -> list[dict]:
        """待处理积压：reply_state='waiting'，按到达顺序。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? AND reply_state=? "
                "AND role='user' ORDER BY id ASC LIMIT ?",
                (session_id, REPLY_BACKLOG, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_backlog_ids(self, session_id: str, exclude_msg_id: str | None = None) -> list[str]:
        """本会话全部待处理消息的 msg_id（用于按上限读入后统一置已读）。"""
        sql = ("SELECT msg_id FROM messages WHERE session_id=? AND reply_state=? "
               "AND role='user' AND msg_id IS NOT NULL")
        params: list[str] = [session_id, REPLY_BACKLOG]
        if exclude_msg_id:
            sql += " AND msg_id<>?"
            params.append(exclude_msg_id)
        with self._lock, self._conn() as c:
            return [r["msg_id"] for r in c.execute(sql, params).fetchall()]

    def mark_backlog_read(self, msg_ids: list[str]) -> int:
        """只读不回：读进上下文 + 结束回复需求（上线读积压、在线窗口超上限时最旧的几条）。

        只动还挂着 waiting 的行——已经回过的消息不能被"读一遍"抹掉结果。
        """
        if not msg_ids:
            return 0
        with self._lock, self._conn() as c:
            # 逐条 UPDATE 累加行数：executemany 之后 SELECT changes() 只报最后一条语句
            return sum(
                c.execute(
                    "UPDATE messages SET read_state=?, reply_state=? "
                    "WHERE msg_id=? AND reply_state=?",
                    (READ_READ, REPLY_NONE, m, REPLY_BACKLOG),
                ).rowcount
                for m in msg_ids
            )

    def finish_backlog(self, msg_ids: list[str], reply_state: str) -> None:
        """回合消费掉这批：读进上下文 + 记下结果（answered / failed / skipped）。"""
        if not msg_ids:
            return
        if reply_state not in (REPLY_ANSWERED, REPLY_FAILED, REPLY_SKIPPED):
            raise ValueError(f"回合结果只能是 answered/failed/skipped，收到 {reply_state!r}")
        with self._lock, self._conn() as c:
            c.executemany(
                "UPDATE messages SET read_state=?, reply_state=? "
                "WHERE msg_id=? AND reply_state=?",
                [(READ_READ, reply_state, m, REPLY_BACKLOG) for m in msg_ids],
            )

    def count_backlog(self, session_id: str | None = None) -> int:
        with self._lock, self._conn() as c:
            if session_id:
                return c.execute(
                    "SELECT COUNT(*) FROM messages WHERE reply_state=? AND session_id=?",
                    (REPLY_BACKLOG, session_id),
                ).fetchone()[0]
            return c.execute(
                "SELECT COUNT(*) FROM messages WHERE reply_state=?", (REPLY_BACKLOG,)
            ).fetchone()[0]

    def reset_chat_data(self) -> dict:
        """清理会话聊天数据：清空 messages + sessions + 出站记账（锚点与发送回执）。
        保留：memory（长期记忆）、config（配置）。
        返回清理统计。"""
        with self._lock, self._conn() as c:
            m = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            s = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM sessions")
            c.execute("DELETE FROM reply_anchors")
            c.execute("DELETE FROM sends")
            return {"messages": m, "sessions": s}

    def reset_all_data(self) -> dict:
        """清空全部业务数据：messages + sessions + memories + 出站记账。"""
        with self._lock, self._conn() as c:
            counts = {
                "messages": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "sessions": c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "memories": c.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            }
            for table in ("messages", "sessions", "memories", "reply_anchors", "sends"):
                c.execute(f"DELETE FROM {table}")
            return counts

    # ---------- 出站记账：被动回复锚点与发送回执 ----------

    def get_reply_anchor(self, session_id: str, msg_id: str) -> dict | None:
        """某条消息作为被动回复锚点用到哪了（next_seq = 下一次要占的序号）。"""
        if not session_id or not msg_id:
            return None
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT * FROM reply_anchors WHERE session_id=? AND msg_id=?",
                (session_id, msg_id),
            ).fetchone()
        return dict(row) if row else None

    def save_reply_anchor(self, session_id: str, msg_id: str, *, event_id: str | None,
                          scene: str, next_seq: int, expires_at: str) -> None:
        """记下这一轮真正用到的锚点与下一个序号；同一个锚点只留一行。

        expires_at 只在第一次写这条锚点时落地（ON CONFLICT 不覆盖它）：被动窗口从这条消息
        第一次被回起算，反复回同一条不会把窗口越拖越长。
        """
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO reply_anchors(session_id, msg_id, event_id, scene, next_seq, "
                "expires_at, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(session_id, msg_id) DO UPDATE SET "
                "event_id=excluded.event_id, scene=excluded.scene, "
                "next_seq=excluded.next_seq, updated_at=excluded.updated_at",
                (session_id, msg_id, event_id, scene, int(next_seq), expires_at, _now()),
            )

    def prune_reply_anchors(self) -> int:
        """删掉已经过期的锚点（过了被动窗口，next_seq 再排下去也没用）。"""
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM reply_anchors WHERE expires_at<?", (_now(),))
            return c.execute("SELECT changes()").fetchone()[0]

    def add_send(self, session_id: str | None, *, msg_id: str | None, msg_seq: int | None,
                 via: str, kind: str, ok: bool, detail: str = "") -> None:
        """一条发送回执：msg_id 是这次挂着的被动锚点（主动发送为空），via 记走的路。"""
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO sends(session_id, msg_id, msg_seq, via, kind, ok, detail, ts) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (session_id, msg_id, msg_seq, via, kind, 1 if ok else 0, detail[:200], _now()),
            )

    def recent_sends(self, session_id: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM sends"
        params: list = []
        if session_id:
            sql += " WHERE session_id=?"
            params.append(session_id)
        params.append(limit)
        with self._lock, self._conn() as c:
            rows = c.execute(sql + " ORDER BY id DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in reversed(rows)]

    def prune_sends(self, keep: int = 2000) -> int:
        """回执只留最近 keep 条：它是排查用的流水，不是要长期攒的账。"""
        with self._lock, self._conn() as c:
            c.execute(
                "DELETE FROM sends WHERE id < (SELECT COALESCE(MAX(id), 0) - ? FROM sends)",
                (int(keep),),
            )
            return c.execute("SELECT changes()").fetchone()[0]

    # ---------- 权限 ----------

    def get_actor(self, openid: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM actors WHERE openid=?", (openid,)).fetchone()
        return dict(row) if row else None

    def set_actor(
        self, openid: str, level: int, name: str | None = None, note: str | None = None
    ) -> dict:
        """写入/更新成员等级；未提供的字段保持原值。"""
        with self._lock, self._conn() as c:
            old = c.execute("SELECT name, note FROM actors WHERE openid=?", (openid,)).fetchone()
            c.execute(
                "INSERT INTO actors(openid, name, level, note, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(openid) DO UPDATE SET level=excluded.level, name=excluded.name, "
                "note=excluded.note, updated_at=excluded.updated_at",
                (
                    openid,
                    name if name is not None else (old["name"] if old else None),
                    int(level),
                    note if note is not None else (old["note"] if old else None),
                    _now(),
                ),
            )
            row = c.execute("SELECT * FROM actors WHERE openid=?", (openid,)).fetchone()
        return dict(row)

    def list_actors(self) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM actors ORDER BY level DESC, updated_at DESC").fetchall()
        return [dict(r) for r in rows]

    def list_senders(self, limit: int = 200) -> list[dict]:
        """最近发言过的成员（含昵称与消息数），用于权限页展示与分配。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                """
                SELECT m.sender, m.sender_name, g.n AS messages, m.ts AS last_ts
                FROM messages m
                JOIN (
                    SELECT sender, MAX(id) AS mid, COUNT(*) AS n
                    FROM messages
                    WHERE role='user' AND sender IS NOT NULL AND sender <> ''
                    GROUP BY sender
                ) g ON m.id = g.mid
                ORDER BY m.id DESC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- 日程（提醒） ----------

    def add_reminder(self, session_id: str, owner: str | None, owner_name: str | None,
                     notify: str | None, notify_name: str | None, due_ts: int, text: str,
                     rule: str = "", times: int = 1) -> int:
        """落一条待触发的日程；due_ts 是下一次到期的 unix 秒（跨时区/重启都比较得动）。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO reminders(session_id, owner, owner_name, notify, notify_name, "
                "due_ts, text, status, created_at, rule, times) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (session_id, owner, owner_name, notify, notify_name,
                 int(due_ts), text, "pending", _now(), rule or "", max(0, int(times))),
            )
            return int(cur.lastrowid)

    def get_reminder(self, rid: int) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM reminders WHERE id=?", (int(rid),)).fetchone()
        return dict(row) if row else None

    def list_reminders(self, session_id: str | None = None, status: str | None = "pending",
                       limit: int = 50, offset: int = 0, q: str | None = None) -> list[dict]:
        """按会话/状态/关键词筛；待触发的按到期时间从近到远，其余按最后一次落定的时间从新到旧。"""
        where, params = _reminder_filter(session_id, status, q)
        order = ("due_ts ASC" if status == "pending" else
                 "COALESCE(last_fired_at, fired_at, created_at) DESC, due_ts DESC")
        sql = f"SELECT * FROM reminders{where} ORDER BY {order} LIMIT ? OFFSET ?"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, (*params, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows]

    def count_reminders(self, session_id: str | None = None, status: str | None = "pending",
                        q: str | None = None) -> int:
        """和 list_reminders 同一份条件——面板要靠它算「还有几页」，不能用另一套口径。"""
        where, params = _reminder_filter(session_id, status, q)
        with self._lock, self._conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM reminders{where}", params).fetchone()[0]

    def reminder_stats(self, now_ts: int, day_start_ts: int) -> dict:
        """一次聚合出面板顶部要的数：各状态条数 + 待触发里逾期/今天要发/周期的条数。"""
        with self._lock, self._conn() as c:
            by_status = {r["status"]: int(r["n"]) for r in c.execute(
                "SELECT status, COUNT(*) AS n FROM reminders GROUP BY status")}
            p = c.execute(
                "SELECT COUNT(*) AS n, "
                "SUM(CASE WHEN due_ts < ? THEN 1 ELSE 0 END) AS overdue, "
                "SUM(CASE WHEN due_ts >= ? AND due_ts < ? THEN 1 ELSE 0 END) AS today, "
                "SUM(CASE WHEN rule <> '' THEN 1 ELSE 0 END) AS periodic "
                "FROM reminders WHERE status='pending'",
                # 今天从「现在」起算：已经过点的那几条记在 overdue 里，不重复计一次
                (int(now_ts), max(int(now_ts), int(day_start_ts)), int(day_start_ts) + 86400),
            ).fetchone()
        total = sum(by_status.values())
        return {
            "total": total,
            "pending": int(p["n"] or 0),
            "overdue": int(p["overdue"] or 0),
            "today": int(p["today"] or 0),
            "periodic": int(p["periodic"] or 0),
            "fired": by_status.get("fired", 0),
            "cancelled": by_status.get("cancelled", 0),
            "expired": by_status.get("expired", 0),
        }

    def due_reminders(self, now_ts: int, limit: int = 20) -> list[dict]:
        """到期待触发的提醒（含停机期间错过的，由调用方按宽限决定发不发）。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM reminders WHERE status='pending' AND due_ts<=? "
                "ORDER BY due_ts ASC LIMIT ?",
                (int(now_ts), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

    def finish_reminder(self, rid: int, status: str, fired_at: str | None = None) -> int:
        """把提醒置为 fired / cancelled / expired；只动 pending 的行，返回真正改到的行数。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET status=?, fired_at=? WHERE id=? AND status='pending'",
                (status, fired_at, int(rid)),
            )
            return cur.rowcount

    def complete_reminder(self, rid: int, fired_at: str) -> int:
        """周期项排满/算不出下一场时的收尾：这一场也算发过，然后置 fired 退场。

        和 finish_reminder 分开，是因为「已发场数」只有在发出去的那条路上才该加一格——
        取消与过期走的还是原来那条。
        """
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET status='fired', fired_at=?, fired_count=fired_count+1, "
                "last_fired_at=? WHERE id=? AND status='pending'",
                (fired_at, fired_at, int(rid)),
            )
            return cur.rowcount

    def reschedule_reminder(self, rid: int, next_due_ts: int, fired_at: str) -> int:
        """周期项发完一场：记一次发出并把到期推到下一场，状态仍是 pending（改不到就说明已被取消）。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET due_ts=?, fired_count=fired_count+1, last_fired_at=? "
                "WHERE id=? AND status='pending'",
                (int(next_due_ts), fired_at, int(rid)),
            )
            return cur.rowcount

    def skip_reminder(self, rid: int, next_due_ts: int) -> int:
        """周期项错过宽限：只把到期推到下一场——错过的那几场不算发过。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE reminders SET due_ts=? WHERE id=? AND status='pending'",
                (int(next_due_ts), int(rid)),
            )
            return cur.rowcount

    def update_reminder(self, rid: int, due_ts: int | None = None, text: str | None = None) -> int:
        """面板改时间/正文：只动还在排的行，终态行改不了（改历史等于伪造发生过的事）。"""
        sets, params = [], []
        if due_ts is not None:
            sets.append("due_ts=?")
            params.append(int(due_ts))
        if text is not None:
            sets.append("text=?")
            params.append(text)
        if not sets:
            return 0
        params.append(int(rid))
        with self._lock, self._conn() as c:
            cur = c.execute(
                f"UPDATE reminders SET {', '.join(sets)} WHERE id=? AND status='pending'", params)
            return cur.rowcount

    def delete_reminder(self, rid: int) -> int:
        """物理删掉一行（面板「删除」和 CLI 用）；取消才是让它不再触发的动作，删除只清记录。"""
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM reminders WHERE id=?", (int(rid),))
            return cur.rowcount

    def delete_terminal_before(self, cutoff: str) -> int:
        """删掉落定时间早于 cutoff 的终态行（保留天数到了就退场，pending 永不参与）。"""
        with self._lock, self._conn() as c:
            cur = c.execute(
                "DELETE FROM reminders WHERE status<>'pending' "
                "AND COALESCE(last_fired_at, fired_at, created_at) <= ?", (cutoff,))
            return cur.rowcount

    def delete_oldest_terminal(self, n: int) -> int:
        """从最旧的终态行开始删 n 条，用于总量上限腾地方。"""
        if n <= 0:
            return 0
        with self._lock, self._conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM reminders WHERE status<>'pending' "
                "ORDER BY COALESCE(last_fired_at, fired_at, created_at) ASC LIMIT ?", (int(n),))]
            if not ids:
                return 0
            c.executemany("DELETE FROM reminders WHERE id=?", [(i,) for i in ids])
            return len(ids)

    def purge_reminders(self, session_id: str | None = None, status: str | None = None,
                        before_ts: int | None = None) -> int:
        """面板/CLI 的批量清理：只碰终态行；条件全空时删 0 条（整库清空不靠这条路）。"""
        if not any((session_id, status, before_ts)):
            return 0
        if status == "pending":
            return 0  # 待触发的行要退场得走「取消」，清理不替人删掉还没发出去的东西
        where = ["status<>'pending'"]
        params: list = []
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        if status:
            where.append("status=?")
            params.append(status)
        if before_ts:
            where.append("due_ts<=?")
            params.append(int(before_ts))
        sql = " AND ".join(where)
        with self._lock, self._conn() as c:
            n = c.execute(f"SELECT COUNT(*) FROM reminders WHERE {sql}", params).fetchone()[0]
            c.execute(f"DELETE FROM reminders WHERE {sql}", params)
        return n

    # ---------- 记忆 ----------

    def add_memory(
        self,
        session_id: str | None,
        content: str,
        type_: str = "profile",
        importance: int = 1,
        embedding: bytes | None = None,
        subject: str | None = None,
        expires_at: str | None = None,
    ) -> str:
        mid = uuid.uuid4().hex[:16]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO memories(id, session_id, type, content, embedding, importance, ts,"
                " subject, expires_at, hit_count) VALUES(?,?,?,?,?,?,?,?,?,0)",
                (mid, session_id, type_, content, embedding, importance, _now(),
                 subject_key(subject), expires_at),
            )
        return mid

    def get_memory(self, memory_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return dict(row) if row else None

    def list_memories(self, session_id: str | None = None, limit: int = 200, offset: int = 0,
                      subject: str | None = None, kind: str | None = None) -> list[dict]:
        where, params = _memory_filter(session_id, subject, kind)
        sql = f"SELECT * FROM memories{where} ORDER BY ts DESC LIMIT ? OFFSET ?"
        with self._lock, self._conn() as c:
            rows = c.execute(sql, (*params, limit, offset)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            out.append(d)
        return out

    def count_memories(self, session_id: str | None = None, subject: str | None = None,
                       kind: str | None = None) -> int:
        where, params = _memory_filter(session_id, subject, kind)
        with self._lock, self._conn() as c:
            return c.execute(f"SELECT COUNT(*) FROM memories{where}", params).fetchone()[0]

    def get_all_memories(self) -> list[dict]:
        """全部记忆（含 embedding 字节），供向量检索与遗忘扫描。"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM memories").fetchall()
        return [dict(r) for r in rows]

    def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        importance: int | None = None,
        embedding: bytes | None = None,
        subject: str | None = None,
        expires_at: str | None = None,
        type_: str | None = None,
    ) -> None:
        """按字段更新；subject / expires_at 允许写成空值（清空归属或到期时间），其余字段 None 表示不动。"""
        with self._lock, self._conn() as c:
            if content is not None:
                c.execute("UPDATE memories SET content=? WHERE id=?", (content, memory_id))
            if importance is not None:
                c.execute("UPDATE memories SET importance=? WHERE id=?", (importance, memory_id))
            if embedding is not None:
                c.execute("UPDATE memories SET embedding=? WHERE id=?", (embedding, memory_id))
            if type_ is not None:
                c.execute("UPDATE memories SET type=? WHERE id=?", (type_, memory_id))
            if subject is not None:
                c.execute("UPDATE memories SET subject=? WHERE id=?", (subject_key(subject), memory_id))
            if expires_at is not None:
                c.execute("UPDATE memories SET expires_at=? WHERE id=?",
                          (expires_at or None, memory_id))

    def touch_memories(self, memory_ids: list[str]) -> None:
        """命中强化：被检索并采用过的记忆刷新时间与次数（遗忘扫描按这个时间算冷却）。"""
        if not memory_ids:
            return
        with self._lock, self._conn() as c:
            c.executemany(
                "UPDATE memories SET hit_count=hit_count+1, last_hit_at=? WHERE id=?",
                [(_now(), m) for m in memory_ids],
            )

    def delete_memory(self, memory_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def delete_memories_by(self, session_id: str | None = None, subject: str | None = None,
                           kind: str | None = None) -> int:
        """按作用域整组删除（面板「清掉这一组」）。三个条件全空时删 0 条——清空整库只走 reset。"""
        where, params = _memory_filter(session_id, subject, kind)
        if not where:
            return 0
        with self._lock, self._conn() as c:
            n = c.execute(f"SELECT COUNT(*) FROM memories{where}", params).fetchone()[0]
            c.execute(f"DELETE FROM memories{where}", params)
        return n

    def delete_memories(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("DELETE FROM memories WHERE id=?", [(m,) for m in memory_ids])

    # ---------- 面板辅助 ----------

    def recent_messages_global(
        self, limit: int = 100, session_id: str | None = None, role: str | None = None
    ) -> list[dict]:
        where, params = [], []
        if session_id:
            where.append("m.session_id=?")
            params.append(session_id)
        if role:
            where.append("m.role=?")
            params.append(role)
        sql = (
            "SELECT m.*, s.key AS skey FROM messages m JOIN sessions s ON s.id=m.session_id "
            + ("WHERE " + " AND ".join(where) + " " if where else "")
            + "ORDER BY m.id DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def stats(self) -> dict:
        """面板概览统计。"""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock, self._conn() as c:
            one = lambda sql, p=(): c.execute(sql, p).fetchone()[0]  # noqa: E731
            return {
                "sessions": one("SELECT COUNT(*) FROM sessions"),
                "messages": one("SELECT COUNT(*) FROM messages"),
                "messages_today": one("SELECT COUNT(*) FROM messages WHERE ts LIKE ?", (today + "%",)),
                "waiting": one("SELECT COUNT(*) FROM messages WHERE reply_state=?", (REPLY_BACKLOG,)),
                "memories": one("SELECT COUNT(*) FROM memories"),
                "reminders_pending": one("SELECT COUNT(*) FROM reminders WHERE status='pending'"),
            }
