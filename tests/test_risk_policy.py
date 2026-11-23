"""风险策略本身：多规则命中时的优先级、批量目标、以及策略与清单的耦合。"""

from __future__ import annotations

from conftest import CATALOG, FixedParser, make_engine

from hvsl.models import DecisionKind, PlannedCall, Provenance, RiskLevel
from hvsl.tools import build_default_registry


def entities_by_id(backend):
    return {entity.entity_id: entity for entity in backend.entities()}


def test_deny_beats_confirm_when_two_rules_match(backend, policy):
    """批量目标里同时命中「开燃气=拒绝」和「大功率=确认」时，必须取更严的一侧。"""
    ids = entities_by_id(backend)
    mixed = [ids["switch.heater_socket"], ids["switch.gas_valve"]]
    risk, risk_class, _reason = policy.classify("hvs.switch.turn_on", mixed)
    assert risk is RiskLevel.DENY
    assert risk_class == "gas_supply_open"

    only_safe = [ids["switch.heater_socket"], ids["switch.kettle_socket"]]
    risk2, class2, _ = policy.classify("hvs.switch.turn_on", only_safe)
    assert risk2 is RiskLevel.CONFIRM
    assert class2 == "high_power_socket_on"


def test_plain_device_matches_no_rule_and_stays_low(backend, policy):
    entity = entities_by_id(backend)["switch.humidifier"]
    risk, risk_class, _ = policy.classify("hvs.switch.turn_on", [entity])
    assert risk is RiskLevel.LOW
    assert risk_class == ""


def test_locking_is_low_but_unlocking_is_confirm(backend, policy):
    lock = entities_by_id(backend)["lock.front_door"]
    assert policy.classify("hvs.lock.lock", [lock])[0] is RiskLevel.LOW
    assert policy.classify("hvs.lock.unlock", [lock])[0] is RiskLevel.CONFIRM


def test_arm_is_low_but_disarm_is_confirm(backend, policy):
    panel = entities_by_id(backend)["alarm_control_panel.home"]
    assert policy.classify("hvs.alarm.arm_away", [panel])[0] is RiskLevel.LOW
    assert policy.classify("hvs.alarm.disarm", [panel])[0] is RiskLevel.CONFIRM


def test_batch_target_including_the_gas_valve_is_refused_entirely():
    """「打开所有插座」里混着燃气电磁阀：整批拒绝，一个都不下发。"""
    engine = make_engine(parser=FixedParser([PlannedCall(tool="hvs.switch.turn_on", target="插座", all=True)]))
    decision = engine.handle("打开所有插座")
    assert decision.kind is DecisionKind.REJECTED
    assert decision.risk_level is RiskLevel.DENY
    assert engine.backend.dispatches == []
    assert len(decision.steps[0].targets) >= 4, "批量目标应覆盖清单里的插座类实体"
    engine.store.close()


def test_every_danger_rule_is_reachable_from_the_catalog(policy, backend, registry):
    """策略声明的每个危险类别都必须有真实可下发的实体落在上面，否则就是死规则。"""
    reachable = set()
    for entity in backend.entities():
        for tool in registry.all():
            if tool.ha_service[0] != entity.domain:
                continue
            risk, risk_class, _ = policy.classify(tool.name, [entity])
            if risk is not RiskLevel.LOW and risk_class:
                reachable.add(risk_class)
    declared = set(policy.danger_classes())
    assert declared == reachable, f"死规则 {sorted(declared - reachable)}；未被策略覆盖 {sorted(reachable - declared)}"


def test_policy_prompts_name_the_target_placeholder(policy):
    for rule in policy.rules:
        if rule.risk is RiskLevel.CONFIRM:
            assert rule.prompt, f"确认类规则 {rule.id} 缺少提示语"
            assert "{{targets}}" in rule.prompt
        if rule.risk is RiskLevel.DENY:
            assert rule.reason, f"拒绝类规则 {rule.id} 必须写清理由"


def test_backend_provenance_matches_the_catalog(backend):
    """真实/模拟标注不能在后端里被改写（A6）。"""
    import json
    from pathlib import Path

    catalog = json.loads(Path(CATALOG).read_text(encoding="utf-8"))["devices"]
    by_id = {entity.entity_id: entity for entity in backend.entities()}
    for spec in catalog:
        assert by_id[spec["entity_id"]].provenance.value == spec.get("provenance", "simulated")
    assert all(entity.provenance is Provenance.SIMULATED for entity in backend.entities()), (
        "本轮清单应全部为模拟实体；改成真实设备前先过安全评审"
    )
