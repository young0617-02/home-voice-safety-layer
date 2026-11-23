"""F-009 audit trail: every decision records intent + targets + risk + confirmation + result.

P0-6 adds an export surface: the trail can be downloaded as CSV or JSON, with
stable headers, optional time/trace filters and utterance redaction by default
(the spoken sentence is the most sensitive column).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from .config import AuditRecord
from .store import Store

#: Stable CSV header - consumers depend on it, so never reorder.
AUDIT_CSV_COLUMNS = (
    "trace_id",
    "seq",
    "session_id",
    "ts",
    "stage",
    "utterance",
    "intent",
    "tool",
    "targets",
    "risk_level",
    "risk_class",
    "confirmation_state",
    "dispatch_state",
    "idempotency_key",
    "dedup",
    "nlu_provider",
    "degraded",
    "latency_ms",
    "detail",
    "error",
)

#: Placeholder used when ``redact_utterance`` is on.
REDACTED_UTTERANCE = "[redacted]"

_INSERT = """
INSERT INTO audit (trace_id, seq, session_id, ts, stage, utterance, intent, tool, targets,
                   risk_level, risk_class, confirmation_state, dispatch_state, idempotency_key,
                   dedup, nlu_provider, degraded, latency_ms, detail, error)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_SELECT_WHERE = """
SELECT * FROM audit
{where}
ORDER BY ts DESC, id DESC
LIMIT ?
"""


class AuditLog:
    def __init__(self, store: Store) -> None:
        self.store = store

    def append(self, record: AuditRecord) -> int:
        cur = self.store.execute(
            _INSERT,
            (
                record.trace_id,
                record.seq,
                record.session_id,
                record.ts,
                record.stage,
                record.utterance,
                record.intent,
                record.tool,
                Store.dumps(record.targets),
                record.risk_level,
                record.risk_class,
                record.confirmation_state,
                record.dispatch_state,
                record.idempotency_key,
                1 if record.dedup else 0,
                record.nlu_provider,
                1 if record.degraded else 0,
                record.latency_ms,
                Store.dumps(record.detail),
                record.error,
            ),
        )
        return int(cur.lastrowid or 0)

    def finalize(
        self,
        row_id: int,
        dispatch_state: str,
        *,
        error: str = "",
        latency_ms: float | None = None,
        dedup: bool | None = None,
    ) -> None:
        """Write the outcome back onto the pre-dispatch row (F-008 "下发后回写状态")."""
        sets = ["dispatch_state = ?", "error = ?"]
        params: list[Any] = [dispatch_state, error]
        if latency_ms is not None:
            sets.append("latency_ms = ?")
            params.append(latency_ms)
        if dedup is not None:
            sets.append("dedup = ?")
            params.append(1 if dedup else 0)
        params.append(row_id)
        self.store.execute(f"UPDATE audit SET {', '.join(sets)} WHERE id = ?", tuple(params))

    def query(
        self,
        *,
        trace_id: str | None = None,
        session_id: str | None = None,
        stage: str | None = None,
        dispatch_state: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100,
    ) -> list[AuditRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("trace_id", trace_id),
            ("session_id", session_id),
            ("stage", stage),
            ("dispatch_state", dispatch_state),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.store.query(_SELECT_WHERE.format(where=where), tuple(params))
        return [self._to_record(row) for row in rows]

    def query_for_export(
        self,
        *,
        trace_id: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 100000,
    ) -> list[AuditRecord]:
        """Export order (oldest first) with the same optional filters as :meth:`query`."""
        clauses: list[str] = []
        params: list[Any] = []
        if trace_id:
            clauses.append("trace_id = ?")
            params.append(trace_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        rows = self.store.query(
            f"SELECT * FROM audit {where} ORDER BY ts ASC, id ASC LIMIT ?", tuple(params)
        )
        return [self._to_record(row) for row in rows]

    def by_trace(self, trace_id: str) -> list[AuditRecord]:
        """Reverse lookup used by A1/A2 evidence: everything we know about one request."""
        rows = self.store.query("SELECT * FROM audit WHERE trace_id = ? ORDER BY id ASC", (trace_id,))
        return [self._to_record(row) for row in rows]

    @staticmethod
    def _to_record(row) -> AuditRecord:

        return AuditRecord(
            trace_id=row["trace_id"],
            seq=row["seq"],
            session_id=row["session_id"],
            ts=row["ts"],
            stage=row["stage"],
            utterance=row["utterance"],
            intent=row["intent"],
            tool=row["tool"],
            targets=json.loads(row["targets"] or "[]"),
            risk_level=row["risk_level"],
            risk_class=row["risk_class"],
            confirmation_state=row["confirmation_state"],
            dispatch_state=row["dispatch_state"],
            idempotency_key=row["idempotency_key"],
            dedup=bool(row["dedup"]),
            nlu_provider=row["nlu_provider"],
            degraded=bool(row["degraded"]),
            latency_ms=row["latency_ms"],
            detail=json.loads(row["detail"] or "{}"),
            error=row["error"],
        )

    def dispatch_count(self, idempotency_key: str) -> int:
        row = self.store.query_one(
            "SELECT dispatch_count, suppressed_count FROM idempotency WHERE key = ?", (idempotency_key,)
        )
        return int(row["dispatch_count"]) if row else 0

    def stats(self) -> dict[str, int]:
        rows: Iterable = self.store.query(
            "SELECT dispatch_state, COUNT(*) AS n FROM audit GROUP BY dispatch_state"
        )
        return {str(r["dispatch_state"]): int(r["n"]) for r in rows}

    def count(self) -> int:
        """Cheap row count used by retention/export tests and operators."""
        row = self.store.query_one("SELECT COUNT(*) AS n FROM audit")
        return int(row["n"]) if row is not None else 0


def _redact_value(value: Any, original: str) -> Any:
    """Recursively redact the original utterance inside nested detail payloads."""
    if isinstance(value, str):
        return REDACTED_UTTERANCE if original and original in value else value
    if isinstance(value, dict):
        return {key: _redact_value(item, original) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, original) for item in value]
    return value


def _record_to_export_row(record: AuditRecord, *, redact_utterance: bool) -> dict[str, Any]:
    """JSON-friendly row; CSV flattens list/dict columns at write time."""
    row = record.model_dump()
    if redact_utterance:
        original = str(row.get("utterance") or "")
        row["utterance"] = REDACTED_UTTERANCE
        if original and original != REDACTED_UTTERANCE:
            row["detail"] = _redact_value(row.get("detail"), original)
    return row


def export_records(
    records: list[AuditRecord],
    destination: str | Path,
    *,
    fmt: str = "csv",
    redact_utterance: bool = True,
) -> int:
    """Write records to ``destination``; returns the number of exported rows.

    CSV always uses :data:`AUDIT_CSV_COLUMNS` so downstream spreadsheets keep a
    stable schema; JSON is a plain array of record objects.
    """
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = (fmt or "csv").lower()
    rows = [_record_to_export_row(record, redact_utterance=redact_utterance) for record in records]
    if fmt == "json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif fmt == "csv":
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=AUDIT_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        **row,
                        "targets": json.dumps(row["targets"], ensure_ascii=False),
                        "detail": json.dumps(row["detail"], ensure_ascii=False, sort_keys=True),
                        "dedup": int(bool(row["dedup"])),
                        "degraded": int(bool(row["degraded"])),
                    }
                )
    else:
        raise ValueError(f"unsupported export format: {fmt!r}")
    return len(rows)
