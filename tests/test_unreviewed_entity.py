"""F1 回归：未评审实体（不在 ``devices/devices.json``）的危险域动作不得默认 low 直发。

红线：HA 动态发现但未进入评审清单的设备（燃气阀、门锁、大功率插座……）若沿用
``default_risk=low``，就会在没有确认的情况下被下发。这里把这个兜底钉死：

* 未评审 + 危险域 -> 至少 ``confirm``（可配置为 ``deny``）；
* 已评审实体行为不变（低风险照常执行）；
* 兜底发生在 :meth:`RiskPolicy.classify` 内部，规则解析 / cross_check 都绕不过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import CATALOG, POLICY, FixedParser, make_engine, step

from hvsl.backends.simulated import SimulatedBackend
from hvsl.errors import PolicyError
from hvsl.models import DecisionKind, DeviceEntity, RiskLevel
from hvsl.risk import UNREVIEWED_RISK_CLASS, RiskPolicy


def _unreviewed(entity_id: str, domain: str, display_name: str, **kwargs) -> DeviceEntity:
    return DeviceEntity(
        entity_id=entity_id,
        domain=domain,
        display_name=display_name,
        reviewed=False,
        **kwargs,
    )


def _engine(entities: list[DeviceEntity], planned, *, policy: RiskPolicy | None = None, parser_cls=FixedParser):
    backend = SimulatedBackend(entities)
    engine = make_engine(backend=backend, parser=parser_cls([planned]), policy=policy)
    return backend, engine


def _policy_with_unreviewed_risk(level: str) -> RiskPolicy:
    data = json.loads(Path(POLICY).read_text(encoding="utf-8"))
    data["unreviewed_risk"] = level
    return RiskPolicy.from_dict(data)


def test_unreviewed_gas_valve_turn_on_is_parked_behind_confirmation():
    """未评审 switch：开阀没有规则命中（无 danger 标签）时必须被兜底成 confirm。"""
    backend, engine = _engine(
        [_unreviewed("switch.gas_valve", "switch", "未评审燃气阀")],
        step("hvs.switch.turn_on", "switch.gas_valve"),
    )
    decision = engine.handle("打开未评审燃气阀")
    assert decision.kind is DecisionKind.AWAITING_CONFIRMATION, decision.message
    assert decision.dispatch_count == 0
    assert backend.dispatch_count == 0, "未评审实体在确认前被下发了"
    assert decision.risk_level is RiskLevel.CONFIRM
    assert decision.steps[0].risk_class == UNREVIEWED_RISK_CLASS

    confirm_rows = engine.audit.query(stage="confirm")
    assert confirm_rows, "兜底拦截必须在 audit 留下 stage=confirm 的记录"
    assert confirm_rows[0].confirmation_state == "pending"
    assert confirm_rows[0].risk_class == UNREVIEWED_RISK_CLASS
    engine.store.close()


def test_unreviewed_lock_unlock_is_parked_behind_confirmation():
    backend, engine = _engine(
        [_unreviewed("lock.front_door", "lock", "未评审入户门锁")],
        step("hvs.lock.unlock", "lock.front_door"),
    )
    decision = engine.handle("解锁未评审入户门锁")
    assert decision.kind is DecisionKind.AWAITING_CONFIRMATION, decision.message
    assert decision.dispatch_count == 0
    assert backend.dispatch_count == 0
    # 既有 door_unlock 规则已经是 confirm，同档时保留更具体的规则类别（只升不降）。
    assert decision.steps[0].risk_class == "door_unlock"
    engine.store.close()


def test_reviewed_catalog_entity_keeps_low_and_still_executes():
    """catalog 内实体 reviewed=True，低风险动作行为不变。"""
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(backend=backend, parser=FixedParser([step("hvs.switch.turn_on", "卧室加湿器")]))
    assert backend.get("switch.humidifier").reviewed is True

    decision = engine.handle("打开加湿器")
    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.dispatch_count == 1
    assert backend.dispatch_count == 1
    assert decision.risk_level is RiskLevel.LOW
    engine.store.close()


def test_unreviewed_risk_deny_rejects_without_dispatch():
    backend, engine = _engine(
        [_unreviewed("switch.gas_valve", "switch", "未评审燃气阀")],
        step("hvs.switch.turn_on", "switch.gas_valve"),
        policy=_policy_with_unreviewed_risk("deny"),
    )
    decision = engine.handle("打开未评审燃气阀")
    assert decision.kind is DecisionKind.REJECTED, decision.message
    assert decision.risk_level is RiskLevel.DENY
    assert decision.dispatch_count == 0
    assert backend.dispatch_count == 0
    assert engine.pending() == [], "被直接拒绝的动作不得进入待确认队列"
    engine.store.close()


def test_existing_higher_risk_is_never_downgraded():
    """未评审实体若已被规则判定为 deny（燃气标签），兜底不能把它降成 confirm。"""
    backend, engine = _engine(
        [_unreviewed("switch.gas_valve", "switch", "未评审燃气阀", tags=["danger:gas"])],
        step("hvs.switch.turn_on", "switch.gas_valve"),
    )
    decision = engine.handle("打开未评审燃气阀")
    assert decision.kind is DecisionKind.REJECTED, decision.message
    assert decision.risk_level is RiskLevel.DENY
    assert backend.dispatch_count == 0
    engine.store.close()


def test_non_danger_domain_unreviewed_entity_is_not_over_blocked():
    """兜底只作用于危险域：未评审的灯仍按低风险执行，避免把系统做成全量确认。"""
    backend, engine = _engine(
        [_unreviewed("light.hall_lamp", "light", "未评审走廊灯")],
        step("hvs.light.turn_on", "light.hall_lamp"),
    )
    decision = engine.handle("打开未评审走廊灯")
    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.dispatch_count == 1
    assert backend.dispatch_count == 1
    engine.store.close()


class _RuleProviderParser(FixedParser):
    """显式声明 provider="rule"：证明来源标签不能绕过兜底。"""

    name = "rule"


def test_rule_provider_cannot_bypass_the_unreviewed_floor():
    backend, engine = _engine(
        [_unreviewed("switch.gas_valve", "switch", "未评审燃气阀")],
        step("hvs.switch.turn_on", "switch.gas_valve"),
        parser_cls=_RuleProviderParser,
    )
    decision = engine.handle("打开未评审燃气阀")
    assert decision.nlu_provider == "rule"
    assert decision.kind is DecisionKind.AWAITING_CONFIRMATION, decision.message
    assert backend.dispatch_count == 0
    engine.store.close()


def test_cross_check_does_not_bypass_the_unreviewed_floor():
    backend = SimulatedBackend([_unreviewed("switch.gas_valve", "switch", "未评审燃气阀")])
    engine = make_engine(
        backend=backend,
        parser=FixedParser([step("hvs.switch.turn_on", "未评审燃气阀")]),
    )
    engine.cross_check = FixedParser([step("hvs.switch.turn_on", "未评审燃气阀")])
    decision = engine.handle("打开未评审燃气阀")
    assert decision.kind is DecisionKind.AWAITING_CONFIRMATION, decision.message
    assert backend.dispatch_count == 0
    engine.store.close()


def test_policy_missing_unreviewed_risk_defaults_to_confirm():
    """旧策略文件没有该字段时默认 confirm（向后兼容且不降低红线）。"""
    data = json.loads(Path(POLICY).read_text(encoding="utf-8"))
    data.pop("unreviewed_risk", None)
    policy = RiskPolicy.from_dict(data)
    assert policy.unreviewed_risk is RiskLevel.CONFIRM
    assert policy.to_dict()["unreviewed_risk"] == "confirm"


def test_policy_refuses_to_lower_unreviewed_risk_to_low():
    """把兜底配成 low 等于重新打开漏洞，加载期必须直接拒绝。"""
    data = json.loads(Path(POLICY).read_text(encoding="utf-8"))
    data["unreviewed_risk"] = "low"
    with pytest.raises(PolicyError, match="unreviewed_risk"):
        RiskPolicy.from_dict(data)