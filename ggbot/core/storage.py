"""SQLite 持久化层：会话、消息、记忆。

全部落盘，重启不丢。线程安全：每个操作一个连接（用完即关）+ WAL，读写都在 self._lock 里串行。
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE,
    scene TEXT,
    summary TEXT,
    archived_upto INTEGER DEFAULT 0,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
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
    status TEXT,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS memories(
    id TEXT PRIMARY KEY,
    session_id TEXT,
    type TEXT,
    content TEXT,
    embedding BLOB,
    importance INTEGER DEFAULT 1,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS actors(
    openid TEXT PRIMARY KEY,
    name TEXT,
    level INTEGER NOT NULL DEFAULT 2,
    note TEXT,
    updated_at TEXT
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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
            self._ensure_columns(c)

    def _ensure_columns(self, c: sqlite3.Connection) -> None:
        """旧库迁移：messages 补齐 event_id / is_at / sender_name，sessions 补归档游标。"""
        cols = {r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
        if "event_id" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN event_id TEXT")
        if "is_at" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN is_at INTEGER DEFAULT 0")
        if "passive_ok" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN passive_ok INTEGER DEFAULT 0")
        if "sender_name" not in cols:
            c.execute("ALTER TABLE messages ADD COLUMN sender_name TEXT")
        scols = {r[1] for r in c.execute("PRAGMA table_info(sessions)").fetchall()}
        if "archived_upto" not in scols:
            c.execute("ALTER TABLE sessions ADD COLUMN archived_upto INTEGER DEFAULT 0")

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
                "INSERT INTO sessions(id, key, scene, summary, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (key, key, scene, "", _now(), _now()),
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

    def save_summary(self, session_id: str, summary: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE sessions SET summary=?, updated_at=? WHERE id=?", (summary, _now(), session_id))

    def get_summary(self, session_id: str) -> str:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT summary FROM sessions WHERE id=?", (session_id,)).fetchone()
            return row["summary"] if row else ""

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
        """窗口外的旧消息：id 在 (after_id, before_id) 之间的记录，按时间正序。"""
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
                """
                SELECT s.id, s.key, s.scene, s.summary, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.status='pending') AS pending_count,
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
                """
                SELECT s.id, s.key, s.scene, s.summary, s.created_at, s.updated_at,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS message_count,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id AND m.status='pending') AS pending_count,
                       (SELECT MAX(m.ts) FROM messages m WHERE m.session_id=s.id) AS last_ts
                FROM sessions s WHERE s.id=?
                """,
                (session_id,),
            ).fetchone()
        return dict(row) if row else None

    def clear_session_context(self, session_id: str) -> dict:
        """清空单个会话的上下文（消息 + 历史摘要），保留会话本身与记忆。"""
        with self._lock, self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,)).fetchone()[0]
            c.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
            c.execute(
                "UPDATE sessions SET summary='', archived_upto=0, updated_at=? WHERE id=?",
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
        status: str = "received",
        sender_name: str | None = None,
    ) -> int:
        if passive_ok is None:
            passive_ok = is_at
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages(session_id, role, sender, sender_name, content, attachments, msg_id, "
                "msg_seq, event_id, is_at, passive_ok, status, ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    status,
                    _now(),
                ),
            )
            return cur.lastrowid

    def message_exists(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        with self._lock, self._conn() as c:
            return c.execute("SELECT 1 FROM messages WHERE msg_id=?", (msg_id,)).fetchone() is not None

    def get_recent_messages(self, session_id: str, limit: int = 50) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

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

    def get_pending_messages(self, session_id: str, limit: int = 1000) -> list[dict]:
        """积压消息：status='pending'，按到达顺序。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? AND status='pending' "
                "AND role='user' ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def list_pending_ids(self, session_id: str, exclude_msg_id: str | None = None) -> list[str]:
        """本会话全部待处理消息的 msg_id（用于按上限读入后统一置 seen）。"""
        sql = ("SELECT msg_id FROM messages WHERE session_id=? AND status='pending' "
               "AND role='user' AND msg_id IS NOT NULL")
        params: list[str] = [session_id]
        if exclude_msg_id:
            sql += " AND msg_id<>?"
            params.append(exclude_msg_id)
        with self._lock, self._conn() as c:
            return [r["msg_id"] for r in c.execute(sql, params).fetchall()]

    def mark_message_status(self, msg_id: str, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE messages SET status=? WHERE msg_id=?", (status, msg_id))

    def mark_pending_processed(self, msg_ids: list[str], status: str = "replied") -> None:
        if not msg_ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("UPDATE messages SET status=? WHERE msg_id=?", [(status, m) for m in msg_ids])

    def reset_chat_data(self) -> dict:
        """清理会话聊天数据：清空 messages + sessions。
        保留：memory（长期记忆）、config（配置）。
        返回清理统计。"""
        with self._lock, self._conn() as c:
            m = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            s = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
            c.execute("DELETE FROM messages")
            c.execute("DELETE FROM sessions")
            return {"messages": m, "sessions": s}

    def reset_all_data(self) -> dict:
        """清空全部业务数据：messages + sessions + memories。"""
        with self._lock, self._conn() as c:
            counts = {
                "messages": c.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
                "sessions": c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "memories": c.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
            }
            for table in ("messages", "sessions", "memories"):
                c.execute(f"DELETE FROM {table}")
            return counts

    def count_pending(self, session_id: str | None = None) -> int:
        with self._lock, self._conn() as c:
            if session_id:
                return c.execute(
                    "SELECT COUNT(*) FROM messages WHERE status='pending' AND session_id=?",
                    (session_id,),
                ).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]

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

    # ---------- 记忆 ----------

    def add_memory(
        self,
        session_id: str | None,
        content: str,
        type_: str = "fact",
        importance: int = 1,
        embedding: bytes | None = None,
    ) -> str:
        mid = uuid.uuid4().hex[:16]
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO memories(id, session_id, type, content, embedding, importance, ts) VALUES(?,?,?,?,?,?,?)",
                (mid, session_id, type_, content, embedding, importance, _now()),
            )
        return mid

    def get_memory(self, memory_id: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return dict(row) if row else None

    def list_memories(self, session_id: str | None = None, limit: int = 200, offset: int = 0) -> list[dict]:
        if session_id:
            sql = "SELECT * FROM memories WHERE session_id=? ORDER BY ts DESC LIMIT ? OFFSET ?"
            params = (session_id, limit, offset)
        else:
            sql = "SELECT * FROM memories ORDER BY ts DESC LIMIT ? OFFSET ?"
            params = (limit, offset)
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            out.append(d)
        return out

    def count_memories(self, session_id: str | None = None) -> int:
        with self._lock, self._conn() as c:
            if session_id:
                return c.execute("SELECT COUNT(*) FROM memories WHERE session_id=?", (session_id,)).fetchone()[0]
            return c.execute("SELECT COUNT(*) FROM memories").fetchone()[0]

    def get_all_memories(self) -> list[dict]:
        """全部记忆（含 embedding 字节），供向量检索。"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM memories").fetchall()
        return [dict(r) for r in rows]

    def update_memory(
        self,
        memory_id: str,
        content: str | None = None,
        importance: int | None = None,
        embedding: bytes | None = None,
    ) -> None:
        with self._lock, self._conn() as c:
            if content is not None:
                c.execute("UPDATE memories SET content=? WHERE id=?", (content, memory_id))
            if importance is not None:
                c.execute("UPDATE memories SET importance=? WHERE id=?", (importance, memory_id))
            if embedding is not None:
                c.execute("UPDATE memories SET embedding=? WHERE id=?", (embedding, memory_id))

    def delete_memory(self, memory_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM memories WHERE id=?", (memory_id,))

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
                "pending": one("SELECT COUNT(*) FROM messages WHERE status='pending'"),
                "memories": one("SELECT COUNT(*) FROM memories"),
            }
