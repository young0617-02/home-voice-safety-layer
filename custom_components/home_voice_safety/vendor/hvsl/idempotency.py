"""F-008 idempotency: a replayed intent inside the window must dispatch only once.

The guard is the only place that decides whether a backend call is allowed. It
writes a row *before* dispatch and the caller writes the outcome back after, so
a crash mid-dispatch leaves the key ``in_flight`` - which is deliberately treated
as "do not send again" rather than "safe to retry", because we cannot know
whether the device received the command.

P0-5 closes two gaps without weakening that property:

* ``stale_seconds`` is now enforced: an ``in_flight`` row older than the threshold
  returns :attr:`GuardResult.STALE_UNKNOWN`. It still **never** returns ``BEGIN``;
* ``failed_unknown`` / ``resolved_unknown`` rows are permanently blocked too, so a
  window expiring can no longer turn an unknown outcome into an automatic re-send.

The human exit is explicit: :meth:`IdempotencyGuard.list_unknown` and
:meth:`IdempotencyGuard.resolve_unknown` only write an audit row - they never call
the backend.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, cast

from .audit import AuditLog
from .config import AuditRecord
from .store import Store

#: Rows whose real-world outcome is unknown. They are (and stay) un-dispatchable.
UNKNOWN_STATUSES = ("in_flight", "failed_unknown")
#: Set by an operator through :meth:`IdempotencyGuard.resolve_unknown`; still blocked.
RESOLVED_UNKNOWN_STATUS = "resolved_unknown"


class GuardResult(str, Enum):
    BEGIN = "begin"  # caller owns the key and may dispatch exactly once
    DUPLICATE = "duplicate"  # same intent already dispatched/handled inside the window
    IN_FLIGHT = "in_flight"  # a previous attempt never reported an outcome - never re-send
    STALE_UNKNOWN = "stale_unknown"  # the unknown row is old enough to be surfaced, still never re-send


class IdempotencyGuard:
    def __init__(self, store: Store, window_seconds: float = 30.0, stale_seconds: float = 120.0) -> None:
        self.store = store
        self.window_seconds = window_seconds
        self.stale_seconds = stale_seconds

    def try_begin(
        self,
        *,
        key: str,
        session_id: str,
        tool: str,
        targets: list[str],
        trace_id: str,
    ) -> GuardResult:
        now = Store.now()

        def fn(conn):
            row = conn.execute("SELECT * FROM idempotency WHERE key = ?", (key,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO idempotency (key, session_id, tool, targets, first_ts, last_ts,"
                    " dispatch_count, suppressed_count, trace_id, status) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, 'in_flight')",
                    (key, session_id, tool, Store.dumps(sorted(targets)), now, now, trace_id),
                )
                return GuardResult.BEGIN

            status = str(row["status"])
            if status == "in_flight":
                age = now - float(row["first_ts"])
                if age > self.stale_seconds:
                    # 陈旧 in-flight：结果仍然未知，只能交给人工出口，绝不 BEGIN。
                    return GuardResult.STALE_UNKNOWN
                conn.execute(
                    "UPDATE idempotency SET suppressed_count = suppressed_count + 1,"
                    " last_ts = ?, trace_id = ? WHERE key = ?",
                    (now, trace_id, key),
                )
                return GuardResult.IN_FLIGHT

            if status in ("failed_unknown", RESOLVED_UNKNOWN_STATUS):
                # 已知的“结果未知”永远不自动重发；窗口过期也不行。
                return GuardResult.STALE_UNKNOWN

            if now - float(row["first_ts"]) < self.window_seconds:
                conn.execute(
                    "UPDATE idempotency SET suppressed_count = suppressed_count + 1,"
                    " last_ts = ?, trace_id = ? WHERE key = ?",
                    (now, trace_id, key),
                )
                return GuardResult.DUPLICATE

            conn.execute(
                "UPDATE idempotency SET first_ts = ?, last_ts = ?, status = 'in_flight',"
                " trace_id = ?, session_id = ?, tool = ?, targets = ? WHERE key = ?",
                (now, now, trace_id, session_id, tool, Store.dumps(sorted(targets)), key),
            )
            return GuardResult.BEGIN

        return cast(GuardResult, self.store.transaction(fn))

    def mark_executed(self, key: str) -> int:
        rows = self.store.execute_returning(
            "UPDATE idempotency SET status = 'executed', dispatch_count = dispatch_count + 1"
            " WHERE key = ? RETURNING dispatch_count",
            (key,),
        )
        return int(rows[0]["dispatch_count"]) if rows else 0

    def mark_failed(self, key: str, *, reached_backend: bool) -> None:
        """Pre-dispatch failures release the key so a genuine retry can happen.

        Anything that reached the device stays blocked: the outcome is unknown and
        a second send is the unsafe option. Rows already resolved by an operator are
        left untouched.
        """
        if reached_backend:
            self.store.execute(
                "UPDATE idempotency SET status = 'failed_unknown'"
                " WHERE key = ? AND status IN ('in_flight', 'failed_unknown')",
                (key,),
            )
        else:
            self.store.execute("DELETE FROM idempotency WHERE key = ? AND status = 'in_flight'", (key,))

    def list_unknown(self, *, now: float | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Every dispatch whose real-world outcome is unknown, oldest first."""
        now = Store.now() if now is None else now
        rows = self.store.query(
            "SELECT * FROM idempotency WHERE status IN ('in_flight', 'failed_unknown')"
            " ORDER BY first_ts ASC LIMIT ?",
            (limit,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            data["targets"] = json.loads(data["targets"] or "[]")
            age = round(now - float(data["first_ts"]), 3)
            data["age_seconds"] = age
            data["stale"] = data["status"] == "in_flight" and age > self.stale_seconds
            out.append(data)
        return out

    def resolve_unknown(self, key: str, note: str = "") -> bool:
        """Operator exit for an unknown outcome: audit it, never re-dispatch it.

        Returns ``False`` when the key is absent or not in an unknown state, so
        callers cannot accidentally "resolve" a normal executed key.
        """
        row = self.store.query_one("SELECT * FROM idempotency WHERE key = ?", (key,))
        if row is None or str(row["status"]) not in UNKNOWN_STATUSES:
            return False
        previous_status = str(row["status"])
        self.store.execute(
            "UPDATE idempotency SET status = ?, last_ts = ? WHERE key = ?",
            (RESOLVED_UNKNOWN_STATUS, Store.now(), key),
        )
        AuditLog(self.store).append(
            AuditRecord(
                trace_id=str(row["trace_id"]),
                session_id=str(row["session_id"]),
                ts=Store.now(),
                stage="result",
                tool=str(row["tool"]),
                targets=json.loads(row["targets"] or "[]"),
                dispatch_state="failed_unknown",
                idempotency_key=key,
                detail={
                    "resolved_unknown": True,
                    "note": note,
                    "previous_status": previous_status,
                    "dispatch_count": int(row["dispatch_count"]),
                },
                error="manual_resolved_unknown",
            )
        )
        return True

    def peek(self, key: str) -> dict[str, Any] | None:
        row = self.store.query_one("SELECT * FROM idempotency WHERE key = ?", (key,))
        if row is None:
            return None
        return dict(row)

    def reset(self) -> None:
        self.store.execute("DELETE FROM idempotency")