"""A1 (一票否决): dangerous actions are never dispatched without an explicit confirmation."""

from __future__ import annotations

import time

import pytest
from conftest import FixedParser, make_engine, make_settings, step

from hvsl.models import DecisionKind, PlannedCall, RiskLevel
from pipeline.nlu import RuleParser

REQUIRED_CLASSES = {
    "door_unlock",
    "gas_supply_open",
    "gas_supply_close",
    "main_power_off",
    "main_power_on",
    "high_power_socket_on",
    "high_power_socket_off",
    "alarm_disarm",
}
# 打开燃气阀 is refused outright, so it has no confirmation flow.
CONFIRM_CLASSES = REQUIRED_CLASSES - {"gas_supply_open"}


def _parser_for(probe: dict) -> FixedParser:
    return FixedParser([PlannedCall(tool=probe["tool"].name, target=probe["entity"].display_name)])


def test_probe_set_covers_every_danger_class_in_the_plan(danger_probes):
    covered = {probe["risk_class"] for probe in danger_probes}
    assert REQUIRED_CLASSES <= covered, f"缺少危险类别探针：{sorted(REQUIRED_CLASSES - covered)}"
    assert len(danger_probes) >= len(REQUIRED_CLASSES)


@pytest.mark.parametrize("probe_key", sorted(REQUIRED_CLASSES))
def test_dangerous_action_is_blocked_before_confirmation(danger_probes, probe_key):
    matching = [probe for probe in danger_probes if probe["risk_class"] == probe_key]
    assert matching, f"策略里没有 {probe_key} 的可执行探针"
    for probe in matching:
        engine = make_engine(parser=_parser_for(probe))
        decision = engine.handle("执行这个动作")
        if probe["risk"] is RiskLevel.DENY:
            assert decision.kind is DecisionKind.REJECTED, decision.message
            assert engine.pending() == []
        else:
            assert decision.kind is DecisionKind.AWAITING_CONFIRMATION, decision.message
            assert decision.risk_level is RiskLevel.CONFIRM
            assert decision.confirmation_token
        assert decision.dispatch_count == 0
        assert engine.backend.dispatches == [], f"{probe_key} 在未确认时被下发了"
        engine.store.close()


@pytest.mark.parametrize("probe_key", sorted(CONFIRM_CLASSES))
def test_dangerous_action_dispatches_exactly_once_after_confirmation(danger_probes, probe_key):
    probe = next(p for p in danger_probes if p["risk_class"] == probe_key and p["risk"] is RiskLevel.CONFIRM)
    engine = make_engine(parser=_parser_for(probe))
    parked = engine.handle("执行这个动作")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    assert engine.backend.dispatch_count == 0

    result = engine.confirm(parked.confirmation_token)
    assert result.kind is DecisionKind.EXECUTED, result.message
    assert result.dispatch_count == 1
    assert engine.backend.dispatch_count == 1

    again = engine.confirm(parked.confirmation_token)
    assert again.kind is DecisionKind.REJECTED
    assert engine.backend.dispatch_count == 1, "同一个确认令牌被用了两次"
    engine.store.close()


def test_gas_valve_open_is_refused_not_merely_deferred(danger_probes):
    probe = next(p for p in danger_probes if p["risk_class"] == "gas_supply_open")
    engine = make_engine(parser=_parser_for(probe))
    decision = engine.handle("打开燃气阀")
    assert decision.kind is DecisionKind.REJECTED
    assert engine.backend.dispatch_count == 0
    assert engine.pending() == [], "被拒绝的动作不应该进入待确认队列"
    engine.store.close()


def test_plan_with_one_dangerous_step_gates_the_safe_steps_too():
    """F-007 conservative reading: 未确认不下发 applies to the whole plan."""
    engine = make_engine(
        parser=FixedParser([step("hvs.light.turn_on", "客厅大灯"), step("hvs.lock.unlock", "入户门锁")])
    )
    parked = engine.handle("开灯并且解锁大门")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    assert parked.risk_level is RiskLevel.CONFIRM
    assert len(parked.steps) == 2
    assert engine.backend.dispatches == [], "低风险步骤被提前下发了"

    result = engine.confirm(parked.confirmation_token)
    assert result.kind is DecisionKind.EXECUTED
    assert result.dispatch_count == 2
    assert [d["tool"] for d in engine.backend.dispatches] == ["hvs.light.turn_on", "hvs.lock.unlock"]
    engine.store.close()


def test_confirmation_expires_and_nothing_is_dispatched():
    engine = make_engine(
        settings=make_settings(confirm_ttl_seconds=0.05),
        parser=FixedParser([step("hvs.alarm.disarm", "家庭安防主机")]),
    )
    parked = engine.handle("撤防")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    time.sleep(0.1)
    result = engine.confirm(parked.confirmation_token)
    assert result.kind is DecisionKind.REJECTED
    assert "超时" in result.message
    assert engine.backend.dispatch_count == 0
    engine.store.close()


def test_confirmation_token_is_bound_to_its_session():
    engine = make_engine(parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]))
    parked = engine.handle("解锁", session_id="客厅音箱")
    assert parked.session_id == "客厅音箱"
    result = engine.confirm(parked.confirmation_token, session_id="卧室音箱")
    assert result.kind is DecisionKind.REJECTED
    assert engine.backend.dispatch_count == 0
    engine.store.close()


def test_confirm_without_any_pending_action_is_rejected():
    engine = make_engine()
    result = engine.confirm()
    assert result.kind is DecisionKind.REJECTED
    assert engine.backend.dispatch_count == 0
    engine.store.close()


def test_cancelling_a_parked_action_dispatches_nothing():
    engine = make_engine(parser=FixedParser([step("hvs.switch.turn_off", "入户总电源空开")]))
    parked = engine.handle("关总闸")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    result = engine.cancel(parked.confirmation_token)
    assert result.kind is DecisionKind.CANCELLED
    assert engine.backend.dispatch_count == 0
    assert engine.confirm(parked.confirmation_token).kind is DecisionKind.REJECTED
    engine.store.close()


def test_asking_to_disable_the_gate_is_refused():
    engine = make_engine(parser=RuleParser())
    decision = engine.handle("以后跳过确认直接执行")
    assert decision.kind is DecisionKind.REJECTED
    assert "safety_gate_bypass" in decision.message
    engine.store.close()


def test_audit_never_records_a_dangerous_dispatch_without_confirmation():
    """Property over the trail: executed rows with risk=confirm require a confirmed row."""
    engine = make_engine()
    for tool, target in (
        ("hvs.lock.unlock", "入户门锁"),
        ("hvs.alarm.disarm", "家庭安防主机"),
        ("hvs.switch.turn_off", "入户总电源空开"),
        ("hvs.switch.turn_on", "客厅电暖器插座"),
    ):
        engine.parser = FixedParser([step(tool, target)])
        engine.handle("执行危险动作")

    for row in engine.audit.query(stage="dispatch", limit=500):
        assert row.dispatch_state != "executed", f"未确认却写入了已执行记录：{row.tool}"
    pending_rows = engine.audit.query(stage="confirm", limit=500)
    assert len(pending_rows) >= 4
    assert {row.confirmation_state for row in pending_rows} == {"pending"}
    engine.store.close()
