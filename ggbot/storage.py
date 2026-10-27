"""SQLite 持久化层：会话、消息、记忆、规则、人设版本。

全部落盘，重启不丢。线程安全：每个操作独立连接 + WAL。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(
    id TEXT PRIMARY KEY,
    key TEXT UNIQUE,
    scene TEXT,
    summary TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS messages(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    sender TEXT,
    content TEXT,
    attachments TEXT,
    msg_id TEXT,
    msg_seq INTEGER,
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
CREATE TABLE IF NOT EXISTS rules(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT,
    trigger TEXT,
    version TEXT,
    active INTEGER DEFAULT 1,
    ts TEXT
);
CREATE TABLE IF NOT EXISTS persona(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT,
    content TEXT,
    version INTEGER,
    operation TEXT,
    diff TEXT,
    ts TEXT,
    source_session TEXT
);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Storage:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._open_conns: list[sqlite3.Connection] = []
        with self._conn() as c:
            c.executescript(SCHEMA)
        # 首次导入内核人设（db/persona_core.md），仅当 persona 表为空
        self._seed_core_persona()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        self._open_conns.append(conn)
        return conn

    def close(self) -> None:
        """关闭全部连接（进程退出/测试清理时调用，释放文件锁）。"""
        with self._lock:
            for conn in self._open_conns:
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass
            self._open_conns.clear()

    def _seed_core_persona(self) -> None:
        """将 persona_core.md 导入内核人设层（AI 只读）。"""
        from ggbot.config import BASE_DIR

        core_file = BASE_DIR / "db" / "persona_core.md"
        if not core_file.exists():
            return
        with self._lock, self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM persona WHERE layer='inner'").fetchone()[0]
            if n == 0:
                c.execute(
                    "INSERT INTO persona(layer, content, version, operation, diff, ts, source_session) "
                    "VALUES(?,?,?,?,?,?,?)",
                    ("inner", core_file.read_text(encoding="utf-8"), 1, "import", "", _now(), "system"),
                )

    # ---------- 会话 ----------

    def get_or_create_session(self, key: str, scene: str = "group") -> str:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT id FROM sessions WHERE key=?", (key,)).fetchone()
            if row:
                c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (_now(), row["id"]))
                return row["id"]
            sid = uuid.uuid4().hex[:16]
            c.execute(
                "INSERT INTO sessions(id, key, scene, summary, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (sid, key, scene, "", _now(), _now()),
            )
            return sid

    def save_summary(self, session_id: str, summary: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE sessions SET summary=?, updated_at=? WHERE id=?", (summary, _now(), session_id))

    def get_summary(self, session_id: str) -> str:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT summary FROM sessions WHERE id=?", (session_id,)).fetchone()
            return row["summary"] if row else ""

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
        status: str = "received",
    ) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO messages(session_id, role, sender, content, attachments, msg_id, msg_seq, status, ts) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    role,
                    sender,
                    content,
                    json.dumps(attachments or [], ensure_ascii=False),
                    msg_id,
                    msg_seq,
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
        return [dict(r) for r in reversed(rows)]

    def get_pending_messages(self, session_id: str, limit: int = 1000) -> list[dict]:
        """积压消息：status='pending'，按到达顺序。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE session_id=? AND status='pending' "
                "AND role='user' ORDER BY id ASC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def mark_message_status(self, msg_id: str, status: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE messages SET status=? WHERE msg_id=?", (status, msg_id))

    def mark_pending_processed(self, msg_ids: list[str], status: str = "replied") -> None:
        if not msg_ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("UPDATE messages SET status=? WHERE msg_id=?", [(status, m) for m in msg_ids])

    def count_pending(self) -> int:
        with self._lock, self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]

    def get_recent_messages_global_pending(self, limit: int = 1000) -> list[dict]:
        """全局 pending 用户消息（按到达顺序），供积压分组扫描。"""
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM messages WHERE status='pending' AND role='user' "
                "ORDER BY id ASC LIMIT ?",
                (limit,),
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

    def list_memories(self, session_id: str | None = None, limit: int = 200) -> list[dict]:
        if session_id:
            sql = "SELECT * FROM memories WHERE session_id=? ORDER BY ts DESC LIMIT ?"
            params = (session_id, limit)
        else:
            sql = "SELECT * FROM memories ORDER BY ts DESC LIMIT ?"
            params = (limit,)
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d.pop("embedding", None)
            out.append(d)
        return out

    def get_all_memories(self) -> list[dict]:
        """全部记忆（含 embedding 字节），供向量检索。"""
        with self._lock, self._conn() as c:
            rows = c.execute("SELECT * FROM memories").fetchall()
        return [dict(r) for r in rows]

    def update_memory(self, memory_id: str, content: str | None = None, importance: int | None = None) -> None:
        with self._lock, self._conn() as c:
            if content is not None:
                c.execute("UPDATE memories SET content=? WHERE id=?", (content, memory_id))
            if importance is not None:
                c.execute("UPDATE memories SET importance=? WHERE id=?", (importance, memory_id))

    def delete_memory(self, memory_id: str) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM memories WHERE id=?", (memory_id,))

    def delete_memories(self, memory_ids: list[str]) -> None:
        if not memory_ids:
            return
        with self._lock, self._conn() as c:
            c.executemany("DELETE FROM memories WHERE id=?", [(m,) for m in memory_ids])

    # ---------- 规则 ----------

    def add_rule(self, content: str, trigger: str | None = None) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO rules(content, trigger, version, active, ts) VALUES(?,?,?,1,?)",
                (content, trigger, "v1", _now()),
            )
            return cur.lastrowid

    def list_rules(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM rules"
        params: tuple = ()
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id ASC"
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def set_rule_active(self, rule_id: int, active: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE rules SET active=? WHERE id=?", (1 if active else 0, rule_id))

    def delete_rule(self, rule_id: int) -> None:
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM rules WHERE id=?", (rule_id,))

    # ---------- 人设 ----------

    def add_persona_version(
        self,
        layer: str,
        content: str,
        operation: str = "append",
        diff: str = "",
        source_session: str | None = None,
    ) -> int:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT MAX(version) AS v FROM persona WHERE layer=?", (layer,)
            ).fetchone()
            version = (row["v"] or 0) + 1
            c.execute(
                "INSERT INTO persona(layer, content, version, operation, diff, ts, source_session) "
                "VALUES(?,?,?,?,?,?,?)",
                (layer, content, version, operation, diff, _now(), source_session),
            )
            return version

    def get_persona(self, layer: str) -> str:
        """该层最新版本内容。"""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT content FROM persona WHERE layer=? ORDER BY version DESC LIMIT 1",
                (layer,),
            ).fetchone()
            return row["content"] if row else ""

    def list_persona_versions(self, layer: str, limit: int = 20) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT * FROM persona WHERE layer=? ORDER BY version DESC LIMIT ?",
                (layer, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def rollback_persona(self, layer: str, version: int) -> str | None:
        """回滚到指定版本：新增一个以该版本内容为准的 rewrite 版本。"""
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT content FROM persona WHERE layer=? AND version=?", (layer, version)
            ).fetchone()
            if not row:
                return None
            content = row["content"]
            current = c.execute(
                "SELECT content FROM persona WHERE layer=? ORDER BY version DESC LIMIT 1",
                (layer,),
            ).fetchone()["content"]
            diff = f"rollback to v{version}"
            v = (c.execute("SELECT MAX(version) AS v FROM persona WHERE layer=?", (layer,)).fetchone()["v"] or 0) + 1
            c.execute(
                "INSERT INTO persona(layer, content, version, operation, diff, ts, source_session) "
                "VALUES(?,?,?,?,?,?,?)",
                (layer, content, v, "rewrite", diff, _now(), "panel"),
            )
        return content

    # ---------- 面板辅助 ----------

    def recent_messages_global(self, limit: int = 100) -> list[dict]:
        with self._lock, self._conn() as c:
            rows = c.execute(
                "SELECT m.*, s.key AS skey FROM messages m JOIN sessions s ON s.id=m.session_id "
                "ORDER BY m.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]
