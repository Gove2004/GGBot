"""kvdb 的存储层：一个自包含的 SQLite 库（tool/kvdb/kv.db），按会话分命名空间。

单独一个库而不是塞进 db/ggbot.db，是为了守住 toolset 自包含：整目录复制就能把数据一起迁移
（和表情素材同一口径）。`*.db` 已被 .gitignore 与部署同步清单全局排除，不用为它加条目。

键按字典序排：带日期的键（`网站价格:2026-11-06`）天然就是时间序，取前缀即得一条日序列。
命名空间就是 session_id，所以群与群、群与私聊之间互不可见，工具侧不接受命名空间参数。
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

DATA_KEY = "kvdb"

SCHEMA = """CREATE TABLE IF NOT EXISTS kv(
    ns TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(ns, key)
)"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _like_prefix(prefix: str) -> str:
    """前缀里的 % 和 _ 是 LIKE 的通配符，要按字面量匹配就得转义（键名里出现下划线很常见）。"""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class KVStore:
    """键值库的唯一读写口。一次操作一个连接（用完即关）+ WAL，读写都在锁里串行。"""

    def __init__(self, rt):
        self.rt = rt
        self._lock = threading.RLock()

    @property
    def db_path(self) -> Path:
        return self.rt.data_dir(DATA_KEY) / "kv.db"

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        """建表语句每次连接都跑一遍（IF NOT EXISTS，表在就是空操作）：
        库文件被从零部署删掉后，下一次调用自己重建，不需要进程重启。
        连接必须自己 close——sqlite3 的连接当上下文管理器只管提交，不关就每次泄漏一对 fd。
        """
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(SCHEMA)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, ns: str, key: str) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute(
                "SELECT key, value, updated_at FROM kv WHERE ns=? AND key=?", (ns, key)
            ).fetchone()
        return dict(row) if row else None

    def put(self, ns: str, key: str, value: str) -> dict:
        """写入并带回旧值：要算增量的一方一次调用就够，不用再 get 一遍。"""
        now = _now()
        with self._lock, self._conn() as c:
            row = c.execute("SELECT value FROM kv WHERE ns=? AND key=?", (ns, key)).fetchone()
            old = row["value"] if row else None
            c.execute(
                "INSERT INTO kv(ns, key, value, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(ns, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (ns, key, value, now),
            )
        return {"old": old, "created": old is None, "updated_at": now}

    def delete(self, ns: str, key: str) -> bool:
        with self._lock, self._conn() as c:
            cur = c.execute("DELETE FROM kv WHERE ns=? AND key=?", (ns, key))
            return cur.rowcount > 0

    def count(self, ns: str) -> int:
        with self._lock, self._conn() as c:
            return int(c.execute("SELECT COUNT(*) FROM kv WHERE ns=?", (ns,)).fetchone()[0])

    def list(self, ns: str, prefix: str = "", limit: int = 50) -> list[dict]:
        sql = "SELECT key, value, updated_at FROM kv WHERE ns=?"
        params: list = [ns]
        if prefix:
            sql += " AND key LIKE ? ESCAPE '\\'"
            params.append(_like_prefix(prefix))
        sql += " ORDER BY key ASC LIMIT ?"
        params.append(max(1, int(limit)))
        with self._lock, self._conn() as c:
            rows = c.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        """收尾：把 WAL 归并回主库，-wal/-shm 随之清空（连接随每次操作关闭，没有长驻连接）。"""
        with self._lock, self._conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")