"""P0-6：审计导出（CSV/JSON）——表头稳定、默认脱敏、可按 trace/时间过滤。"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from conftest import CATALOG, FixedParser, make_engine, step

from custom_components.home_voice_safety.const import SERVICE_EXPORT_AUDIT
from custom_components.home_voice_safety.services import dispatch_request
from hvsl.audit import (
    AUDIT_CSV_COLUMNS,
    REDACTED_UTTERANCE,
    AuditLog,
    export_records,
)
from hvsl.config import AuditRecord
from hvsl.store import Store

SECRET_UTTERANCE = "解锁入户门锁"


def _record(trace_id: str, utterance: str, ts: float, stage: str = "utterance") -> AuditRecord:
    return AuditRecord(
        trace_id=trace_id,
        session_id="客厅",
        ts=ts,
        stage=stage,
        utterance=utterance,
        tool="hvs.lock.unlock",
        targets=["lock.front_door"],
        risk_level="confirm",
        risk_class="door_unlock",
        detail={"pending": True},
    )


def _seeded_log() -> AuditLog:
    store = Store(":memory:")
    log = AuditLog(store)
    log.append(_record("trace-1", SECRET_UTTERANCE, 1000.0))
    log.append(_record("trace-1", "确认执行", 1001.0, stage="confirm"))
    log.append(_record("trace-2", "另一个请求", 1002.0))
    return log


def test_csv_export_has_stable_header_and_redacts_utterance(tmp_path):
    log = _seeded_log()
    records = log.query_for_export(trace_id="trace-1")
    assert [record.trace_id for record in records] == ["trace-1", "trace-1"]

    destination = tmp_path / "audit.csv"
    assert export_records(records, destination, fmt="csv", redact_utterance=True) == 2

    with destination.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
    assert reader.fieldnames == list(AUDIT_CSV_COLUMNS)
    assert len(rows) == 2
    assert all(row["utterance"] == REDACTED_UTTERANCE for row in rows)
    assert SECRET_UTTERANCE not in destination.read_text(encoding="utf-8")


def test_json_export_is_parseable_and_redacts(tmp_path):
    log = _seeded_log()
    records = log.query_for_export(since=1000.5, until=1001.5)
    assert len(records) == 1

    destination = tmp_path / "audit.json"
    assert export_records(records, destination, fmt="json", redact_utterance=True) == 1
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) == 1
    assert payload[0]["utterance"] == REDACTED_UTTERANCE
    assert payload[0]["trace_id"] == "trace-1"
    assert isinstance(payload[0]["targets"], list)


def test_export_can_keep_the_original_utterance_when_explicitly_allowed(tmp_path):
    log = _seeded_log()
    records = log.query_for_export(trace_id="trace-1")
    destination = tmp_path / "audit-plain.csv"
    export_records(records, destination, fmt="csv", redact_utterance=False)
    assert SECRET_UTTERANCE in destination.read_text(encoding="utf-8")
    assert REDACTED_UTTERANCE not in destination.read_text(encoding="utf-8")


def test_export_audit_service_writes_into_the_export_dir(tmp_path):
    engine = make_engine(
        backend=None,
        parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]),
    )
    engine.handle("打开客厅大灯")

    result = dispatch_request(
        engine,
        SERVICE_EXPORT_AUDIT,
        {"format": "csv"},
        export_dir=tmp_path,
    )
    assert result["ok"] is True and result["kind"] == "exported"
    assert result["rows"] >= 1
    exported = Path(result["path"])
    assert exported.is_file()
    assert "打开客厅大灯" not in exported.read_text(encoding="utf-8")

    json_result = dispatch_request(
        engine,
        SERVICE_EXPORT_AUDIT,
        {"format": "json", "redact_utterance": False},
        export_dir=tmp_path,
    )
    assert json_result["ok"] is True and json_result["format"] == "json"
    payload = json.loads(Path(json_result["path"]).read_text(encoding="utf-8"))
    assert any(item["utterance"] == "打开客厅大灯" for item in payload)

    bad = dispatch_request(engine, SERVICE_EXPORT_AUDIT, {"format": "xml"}, export_dir=tmp_path)
    assert bad["ok"] is False
    engine.store.close()