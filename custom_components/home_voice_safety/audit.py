"""F-009 re-exported for Home Assistant, plus HA-friendly views of the trail."""

from __future__ import annotations

from typing import Any

from hvsl.audit import AuditLog
from hvsl.config import AuditRecord
from hvsl.store import Store

__all__ = ["AuditLog", "AuditRecord", "Store", "recent", "by_trace", "as_rows"]


def recent(engine, limit: int = 20) -> list[dict[str, Any]]:
    return [record.model_dump() for record in engine.audit.query(limit=limit)]


def by_trace(engine, trace_id: str) -> list[dict[str, Any]]:
    return [record.model_dump() for record in engine.audit.by_trace(trace_id)]


def as_rows(records: list[AuditRecord]) -> list[list[str]]:
    """Compact table form for a HA sensor attribute."""
    return [
        [
            record.stage,
            record.tool or "-",
            ",".join(record.targets) or "-",
            record.risk_level or "-",
            record.confirmation_state or "-",
            record.dispatch_state or "-",
        ]
        for record in records
    ]
