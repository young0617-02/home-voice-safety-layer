"""P2-2：审计保留期 —— 过期行可清理，但安全证据（unknown/pending/idempotency）永不删除。"""

from __future__ import annotations

import time

import pytest
from conftest import make_engine, make_settings
from pydantic import ValidationError

from custom_components.home_voice_safety.services import SERVICE_PURGE_AUDIT, dispatch_request
from hvsl.audit import AuditLog
from hvsl.config import AuditRecord, Settings
from hvsl.store import Store

NOW = time.time()
DAY = 86400.0
OLD = NOW - 31 * DAY


def _audit(trace_id: str, ts: float, *, dispatch_state: str = "executed",
           idempotency_key: str = "", error: str = "", stage: str = "dispatch") -> AuditRecord:
    return AuditRecord(
        trace_id=trace_id,
        session_id="客厅",
        ts=ts,
        stage=stage,
        dispatch_state=dispatch_state,
        idempotency_key=idempotency_key,
        error=error,
        tool="hvs.lock.unlock",
    )


def _seed(store: Store) -> None:
    log = AuditLog(store)
    log.append(_audit("old-normal", OLD))
    log.append(_audit("old-unknown", OLD, dispatch_state="failed_unknown",
                      idempotency_key="unknown-key", error="stale_unknown"))
    log.append(_audit("old-inflight", OLD, dispatch_state="DISPATCHING", idempotency_key="inflight-key"))
    log.append(_audit("recent", NOW - 1))
    store.execute(
        "INSERT INTO pending (token, session_id, utterance, steps, risk_class, created_at, expires_at, trace_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("tok", "客厅", "解锁大门", "[]", "door_unlock", NOW - 100, NOW + 3600, "trace-pending"),
    )
    store.execute(
        "INSERT INTO idempotency (key, session_id, tool, targets, first_ts, last_ts,"
        " dispatch_count, suppressed_count, trace_id, status) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
        ("unknown-key", "客厅", "hvs.lock.unlock", "[]", OLD, OLD, "trace-unknown", "failed_unknown"),
    )
    store.execute(
        "INSERT INTO idempotency (key, session_id, tool, targets, first_ts, last_ts,"
        " dispatch_count, suppressed_count, trace_id, status) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)",
        ("inflight-key", "客厅", "hvs.lock.unlock", "[]", OLD, OLD, "trace-inflight", "in_flight"),
    )


def test_purge_deletes_only_expired_safe_rows():
    store = Store(":memory:")
    _seed(store)

    deleted = store.purge(NOW - 30 * DAY)
    assert deleted == 1, "只应删除那条普通过期 audit 行"

    remaining = {row["trace_id"] for row in store.query("SELECT trace_id FROM audit")}
    assert remaining == {"old-unknown", "old-inflight", "recent"}
    assert store.query_one("SELECT * FROM pending WHERE token = ?", ("tok",)) is not None, "未过期 pending 被误删"
    assert store.query_one("SELECT * FROM idempotency WHERE key = ?", ("unknown-key",))["status"] == "failed_unknown"
    store.close()


def test_zero_retention_means_keep_forever():
    engine = make_engine(settings=make_settings(audit_retention_days=0))
    engine.audit.append(_audit("old", OLD))
    result = dispatch_request(engine, SERVICE_PURGE_AUDIT, {})
    assert result["ok"] is True
    assert result["deleted"] == 0
    assert result["retention_days"] == 0
    assert engine.audit.count() == 1
    engine.store.close()


def test_service_purges_using_settings_default():
    engine = make_engine(settings=make_settings(audit_retention_days=30))
    engine.audit.append(_audit("old", OLD))
    engine.audit.append(_audit("fresh", NOW - 10))
    result = dispatch_request(engine, SERVICE_PURGE_AUDIT, {})
    assert result["ok"] is True
    assert result["deleted"] == 1
    assert result["retention_days"] == 30
    assert engine.audit.count() == 1
    engine.store.close()


def test_before_argument_takes_precedence_over_days():
    engine = make_engine(settings=make_settings(audit_retention_days=0))
    engine.audit.append(_audit("old", OLD))
    result = dispatch_request(engine, SERVICE_PURGE_AUDIT, {"before": NOW - 30 * DAY, "days": 0})
    assert result["deleted"] == 1
    assert result["retention_days"] is None
    engine.store.close()


def test_negative_days_is_refused_by_the_service():
    engine = make_engine(settings=make_settings(audit_retention_days=30))
    result = dispatch_request(engine, SERVICE_PURGE_AUDIT, {"days": -1})
    assert result["ok"] is False
    assert result["kind"] == "invalid_argument"
    engine.store.close()


def test_negative_retention_is_rejected_at_construction():
    with pytest.raises(ValidationError):
        Settings(audit_retention_days=-1)


def test_negative_retention_env_is_rejected(monkeypatch):
    monkeypatch.setenv("HVSL_AUDIT_RETENTION_DAYS", "-1")
    with pytest.raises(ValueError, match="不能为负数"):
        Settings.load()