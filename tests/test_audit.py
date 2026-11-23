"""F-009: the trail must carry intent + targets + risk + confirmation state + result, and be reverse-searchable."""

from __future__ import annotations

from conftest import CATALOG, FixedParser, make_engine, make_settings, step

from hvsl.backends.simulated import SimulatedBackend, load_catalog
from hvsl.errors import BackendUnavailableError
from hvsl.models import Decision, DecisionKind, Intent


def test_the_audit_row_exists_before_the_backend_call():
    observed: dict[str, object] = {}

    class Pre(SimulatedBackend):
        def call_tool(self, tool, entity_ids, args):
            observed["in_flight"] = engine.audit.query(dispatch_state="DISPATCHING", limit=10)
            observed["backend_state"] = self.get("light.living_room_main").state
            return super().call_tool(tool, entity_ids, args)

    engine = make_engine(
        backend=Pre(load_catalog(CATALOG)),
        parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]),
    )
    decision = engine.handle("开灯")
    rows = observed["in_flight"]
    assert len(rows) == 1, "下发前没有留痕"
    assert rows[0].tool == "hvs.light.turn_on"
    assert rows[0].targets == ["light.living_room_main"]
    assert observed["backend_state"] == "off", "留痕竟晚于设备状态变化"
    assert decision.kind is DecisionKind.EXECUTED
    assert engine.audit.query(dispatch_state="DISPATCHING", limit=10) == [], "结果没有回写"
    executed = engine.audit.query(dispatch_state="executed", limit=10)
    assert len(executed) == 1
    assert executed[0].latency_ms is not None
    engine.store.close()


def test_failure_outcome_is_written_back_with_the_error():
    class Broken(SimulatedBackend):
        def call_tool(self, tool, entity_ids, args):
            raise BackendUnavailableError("设备离线")

    engine = make_engine(backend=Broken(load_catalog(CATALOG)), parser=FixedParser([step("hvs.fan.turn_on", "卧室风扇")]))
    engine.handle("开风扇")
    rows = engine.audit.query(dispatch_state="failed", limit=10)
    assert len(rows) == 1
    assert "设备离线" in rows[0].error
    assert rows[0].risk_level == "low"
    engine.store.close()


def test_a_full_dangerous_flow_is_recoverable_from_one_trace():
    engine = make_engine(parser=FixedParser([step("hvs.alarm.disarm", "家庭安防主机")]))
    parked = engine.handle("撤防")
    engine.confirm(parked.confirmation_token)

    trail = engine.trace(parked.trace_id)
    stages = [record.stage for record in trail]
    assert stages == ["utterance", "nlu", "confirm", "confirm", "dispatch"], stages

    pending_row, confirmed_row, dispatch_row = trail[2], trail[3], trail[4]
    assert pending_row.confirmation_state == "pending"
    assert confirmed_row.confirmation_state == "confirmed"
    assert dispatch_row.dispatch_state == "executed"
    assert dispatch_row.risk_level == "confirm"
    assert dispatch_row.risk_class == "alarm_disarm"
    assert dispatch_row.targets == ["alarm_control_panel.home"]
    assert dispatch_row.intent == "confirmed_plan"
    assert dispatch_row.idempotency_key
    engine.store.close()


def test_rejections_are_audited_with_their_reason():
    engine = make_engine(parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]))
    decision = engine.handle("")
    assert decision.kind is DecisionKind.REJECTED
    rows = engine.audit.query(stage="reject", limit=10)
    assert len(rows) == 1
    assert "空指令" in rows[0].detail.get("reason", "") or "空指令" in rows[0].error
    engine.store.close()


def test_stats_count_dispatches_suppressions_and_pending():
    engine = make_engine(
        settings=make_settings(dedup_window_seconds=30.0),
        parser=FixedParser([step("hvs.light.turn_off", "书房台灯")]),
    )
    engine.handle("关灯")
    engine.handle("关灯")
    stats = engine.stats()
    assert stats["dispatched"] == 1
    assert stats["suppressed"] == 1
    assert stats["pending"] == 0
    engine.store.close()


def test_audit_rows_carry_nlu_provider_and_degradation_flag():
    class DegradedParser:
        def parse(self, utterance: str) -> Intent:
            return Intent(utterance=utterance, provider="llm->rule", degraded=True, steps=[])

    engine = make_engine(parser=DegradedParser())
    engine.handle("随便说点什么")
    nlu_rows = engine.audit.query(stage="nlu", limit=10)
    assert len(nlu_rows) == 1
    assert nlu_rows[0].nlu_provider == "llm->rule"
    assert nlu_rows[0].degraded is True
    engine.store.close()
