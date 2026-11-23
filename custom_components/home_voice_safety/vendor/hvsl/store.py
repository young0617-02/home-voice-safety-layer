"""SQLite storage shared by the audit trail (F-009) and the idempotency guard (F-008)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path

#: R12：关闭原句落盘时写入 audit.utterance 的占位符（schema 不变）。
REDACTED_UTTERANCE = "[redacted]"


def _env_store_utterance() -> bool:
    """Read ``HVSL_STORE_UTTERANCE``; only explicit falsy values disable storage."""
    raw = os.environ.get("HVSL_STORE_UTTERANCE")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    seq INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT 'default',
    ts REAL NOT NULL,
    stage TEXT NOT NULL,
    utterance TEXT NOT NULL DEFAULT '',
    intent TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    targets TEXT NOT NULL DEFAULT '[]',
    risk_level TEXT NOT NULL DEFAULT '',
    risk_class TEXT NOT NULL DEFAULT '',
    confirmation_state TEXT NOT NULL DEFAULT '',
    dispatch_state TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL DEFAULT '',
    dedup INTEGER NOT NULL DEFAULT 0,
    nlu_provider TEXT NOT NULL DEFAULT '',
    degraded INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL,
    detail TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_trace ON audit (trace_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit (ts);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit (session_id, ts);

CREATE TABLE IF NOT EXISTS idempotency (
    key TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    targets TEXT NOT NULL DEFAULT '[]',
    first_ts REAL NOT NULL,
    last_ts REAL NOT NULL,
    dispatch_count INTEGER NOT NULL DEFAULT 0,
    suppressed_count INTEGER NOT NULL DEFAULT 0,
    trace_id TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_ts ON idempotency (last_ts);

-- F-007/F-009：待确认动作跨重启持久化。additive migration：旧库首次启动自动建表。
CREATE TABLE IF NOT EXISTS pending (
    token TEXT PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT 'default',
    utterance TEXT NOT NULL DEFAULT '',
    steps TEXT NOT NULL DEFAULT '[]',
    risk_class TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    trace_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_pending_session ON pending (session_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_pending_expires ON pending (expires_at);
"""


class Store:
    """One connection per Store, serialised by a lock.

    ``BEGIN IMMEDIATE`` plus the lock gives the idempotency guard atomic
    read-modify-write, which is what makes "replay inside the window executes
    once" hold under concurrent requests.
    """

    def __init__(self, db_path: str | Path = ":memory:", *, store_utterance: bool | None = None) -> None:
        self.db_path = str(db_path)
        # R12：默认跟随环境变量；显式传参优先（便于测试/嵌入式调用）。
        self.store_utterance = _env_store_utterance() if store_utterance is None else bool(store_utterance)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, self._redact_sql_params(sql, params))
            self._conn.commit()
            return cur

    def _redact_sql_params(self, sql: str, params: tuple) -> tuple:
        """R12：audit 插入时按开关把 utterance 替换为占位符。

        ``hvsl.audit._INSERT`` 的参数顺序固定：
        trace_id, seq, session_id, ts, stage, **utterance**, intent, ...
        因此第 6 个参数（index 5）就是原句。只对 ``INSERT INTO audit`` 生效，
        不触碰 pending / idempotency；schema 和 trace/risk 等字段保持不变。
        """
        if self.store_utterance:
            return params
        normalized = " ".join(sql.strip().lower().split())
        if not normalized.startswith("insert into audit") or len(params) < 6:
            return params
        redacted = list(params)
        redacted[5] = REDACTED_UTTERANCE
        return tuple(redacted)

    def execute_returning(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        """Run a DML statement with RETURNING in its own immediate transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                rows = self._conn.execute(sql, params).fetchall()
                self._conn.commit()
                return rows
            except Exception:
                self._conn.rollback()
                raise

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def transaction(self, fn) -> object:
        """Run ``fn(conn)`` inside an immediate transaction and commit."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._conn)
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    @staticmethod
    def dumps(value: object) -> str:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)

    def purge(self, before_ts: float, *, vacuum: bool = False) -> int:
        """Delete audit rows older than ``before_ts``; return how many were deleted.

        Safety evidence is never purged:

        * rows whose dispatch state is ``failed_unknown`` / ``in_flight_unknown``;
        * rows tied to an idempotency key that is still ``in_flight`` /
          ``failed_unknown`` / ``resolved_unknown``;
        * the ``pending`` and ``idempotency`` tables are not touched at all.

        ``vacuum=True`` is optional because VACUUM rewrites the database file and
        should not run on every daily pass unless the operator asks for it.
        """
        cursor = self.execute(
            "DELETE FROM audit WHERE ts < ?"
            " AND dispatch_state NOT IN ('failed_unknown', 'in_flight_unknown')"
            " AND error NOT IN ('stale_unknown', 'manual_resolved_unknown')"
            " AND (idempotency_key = '' OR idempotency_key NOT IN ("
            "   SELECT key FROM idempotency"
            "   WHERE status IN ('in_flight', 'failed_unknown', 'resolved_unknown')"
            " ))",
            (before_ts,),
        )
        deleted = int(cursor.rowcount or 0)
        if vacuum:
            self.execute("VACUUM")
        return deleted

    @staticmethod
    def now() -> float:
        return time.time()
