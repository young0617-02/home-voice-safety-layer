"""F-002/F-003: the HA integration's dispatch path, proven against a duck-typed fake hass.

Home Assistant itself is not installed here, so ``backend.py``/``services.py`` are
written against the small slice of the ``hass`` API they actually use
(``states.async_all`` / ``states.get`` / ``services.has_service`` / ``services.call`` /
``services.async_register`` / ``bus.async_fire`` / ``async_add_executor_job``).
This proves the integration logic; the HA *lifecycle* (config flow, setup entry) still
needs a live HA run — recorded in README 的「未覆盖清单」。
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.home_voice_safety import (
    safety as integration_safety,
)
from custom_components.home_voice_safety import (
    services as integration_services,
)
from custom_components.home_voice_safety.backend import HassLocalBackend
from custom_components.home_voice_safety.const import (
    DOMAIN,
    EVENT_CONFIRMATION_REQUIRED,
    SERVICE_CANCEL_ACTION,
    SERVICE_CONFIRM_ACTION,
    SERVICE_HANDLE_UTTERANCE,
    SERVICE_LIST_PENDING,
)
from hvsl.errors import BackendUnavailableError, PreDispatchError
from hvsl.models import DecisionKind, Intent, PlannedCall, Provenance

CATALOG = str(ROOT / "devices" / "devices.json")
POLICY = str(ROOT / "hvsl" / "policy" / "risk_policy.json")

HA_STATES = [
    ("light.living_room_main", "off", {"friendly_name": "客厅大灯"}),
    ("switch.gas_valve", "off", {"friendly_name": "燃气阀（设备自称安全）", "tags": ["safe"]}),
    ("switch.main_breaker", "on", {"friendly_name": "总闸"}),
    ("lock.front_door", "locked", {"friendly_name": "入户门锁"}),
    ("alarm_control_panel.home", "armed_away", {"friendly_name": "家庭安防主机"}),
    ("switch.heater_socket", "off", {"friendly_name": "客厅电暖器插座"}),
    ("cover.living_room_curtain", "closed", {"friendly_name": "客厅窗帘"}),
    ("fan.bedroom_fan", "off", {"friendly_name": "卧室风扇"}),
    ("vacuum.robot", "idle", {"friendly_name": "扫地机器人"}),
    ("media_player.living_room_tv", "on", {"friendly_name": "客厅电视"}),
    ("climate.living_room_ac", "off", {"friendly_name": "客厅空调"}),
    ("switch.humidifier", "off", {"friendly_name": "卧室加湿器"}),
    ("switch.boom", "off", {"friendly_name": "故障插座"}),
    ("light.study_desk", "off", {"friendly_name": "书房台灯"}),
    ("light.bedroom_main", "on", {"friendly_name": "卧室主灯"}),
    ("light.living_room_strip", "off", {"friendly_name": "客厅灯带"}),
    ("climate.bedroom_ac", "off", {"friendly_name": "卧室空调"}),
]


class FakeState:
    def __init__(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes


class FakeStates:
    def __init__(self, states: list[FakeState]) -> None:
        self._states = {state.entity_id: state for state in states}

    def async_all(self) -> list[FakeState]:
        return list(self._states.values())

    def get(self, entity_id: str) -> FakeState | None:
        return self._states.get(entity_id)


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.registered: dict[tuple[str, str], Any] = {}
        self.registration_options: dict[tuple[str, str], dict[str, Any]] = {}
        from hvsl.tools import build_default_registry

        self.known = {tool.ha_service for tool in build_default_registry().all()} | {
            ("home_voice_safety", name) for name in integration_services.SERVICES
        }

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.known

    async def async_call(self, domain: str, service: str, data: dict[str, Any] | None = None,
                         blocking: bool = False, **kwargs: Any) -> None:
        """F2：现代 HA 只提供 async_call；这里刻意不再实现同步 ``call``。"""
        self.calls.append({"domain": domain, "service": service, "data": dict(data or {}), "blocking": blocking})
        # 用 catalog 内的 switch.humidifier 模拟“已送达后失败”：switch.boom 不在
        # devices.json 中（F1 的未评审实体），它会被停在确认阶段，无法到达这里。
        if "switch.humidifier" in str((data or {}).get("entity_id")):
            raise RuntimeError("设备离线")

    def async_register(
        self,
        domain: str,
        service: str,
        handler,
        schema=None,
        supports_response=None,
        **kwargs: Any,
    ) -> None:
        self.registered[(domain, service)] = handler
        self.registration_options[(domain, service)] = {
            "schema": schema,
            "supports_response": supports_response,
        }

    def async_remove(self, domain: str, service: str) -> None:
        self.registered.pop((domain, service), None)
        self.registration_options.pop((domain, service), None)


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def async_fire(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append({"event_type": name, **payload})


class FakeHass:
    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self.loop = loop
        self.states = FakeStates([FakeState(*row) for row in HA_STATES])
        self.services = FakeServices()
        self.bus = FakeBus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeCall:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


def make_engine(loop: asyncio.AbstractEventLoop):
    from hvsl.config import Settings
    from hvsl.engine import DecisionEngine
    from hvsl.risk import RiskPolicy
    from hvsl.store import Store
    from hvsl.tools import build_default_registry
    from pipeline.nlu import RuleParser

    hass = FakeHass(loop)
    settings = Settings(
        db_path=":memory:",
        policy_path=POLICY,
        device_catalog=CATALOG,
        confirm_ttl_seconds=60.0,
        dedup_window_seconds=30.0,
    )
    registry = build_default_registry()
    engine = DecisionEngine(
        HassLocalBackend(hass, catalog_path=CATALOG, real_entities="light.living_room_main"),
        RuleParser(registry),
        settings=settings,
        registry=registry,
        policy=RiskPolicy.load(POLICY),
        store=Store(":memory:"),
    )
    return hass, engine


@pytest.fixture(scope="module")
def ha_loop():
    """HA 的事件循环跑在后台线程：backend 从同步 executor 线程桥接过去（F2）。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


@pytest.fixture()
def ha_engine(ha_loop):
    hass, engine = make_engine(ha_loop)
    yield hass, engine
    engine.store.close()


def test_backend_lists_entities_with_catalog_tags(ha_engine):
    _hass, engine = ha_engine
    entities = {entity.entity_id: entity for entity in engine.backend.entities()}
    assert entities["light.living_room_main"].provenance is Provenance.REAL
    assert entities["switch.gas_valve"].provenance is Provenance.SIMULATED
    assert "danger:gas" in entities["switch.gas_valve"].tags
    assert "danger:security" in entities["lock.front_door"].tags


def test_backend_refuses_to_run_without_a_reviewed_catalog():
    """缺清单 = 危险标签无从判定，宁可不开机（fail closed）。"""
    with pytest.raises(BackendUnavailableError, match="设备清单"):
        HassLocalBackend(FakeHass(), catalog_path=str(ROOT / "does-not-exist.json"))
    with pytest.raises(BackendUnavailableError, match="设备清单"):
        HassLocalBackend(FakeHass(), catalog_path=None)


def test_backend_call_uses_ha_service_names(ha_engine):
    hass, engine = ha_engine
    result = engine.handle("打开客厅大灯")
    assert result.kind is DecisionKind.EXECUTED
    call = hass.services.calls[-1]
    assert (call["domain"], call["service"]) == ("light", "turn_on")
    assert call["data"]["entity_id"] == "light.living_room_main"
    assert call["blocking"] is True


def test_dangerous_action_never_reaches_the_ha_bus_before_confirmation(ha_engine):
    hass, engine = ha_engine
    parked = engine.handle("解锁入户门锁")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    assert hass.services.calls == [], "危险动作在确认前就调用了 HA 服务"

    confirmed = engine.confirm(parked.confirmation_token)
    assert confirmed.kind is DecisionKind.EXECUTED
    assert [(c["domain"], c["service"]) for c in hass.services.calls] == [("lock", "unlock")]


def test_gas_valve_open_is_refused_over_ha_too(ha_engine):
    hass, engine = ha_engine
    decision = engine.handle("打开燃气阀")
    assert decision.kind is DecisionKind.REJECTED
    assert hass.services.calls == []


def test_backend_maps_unknown_service_to_pre_dispatch_error(ha_engine):
    from hvsl.tools import NoArgs, ToolDef

    hass, engine = ha_engine
    with pytest.raises(PreDispatchError):
        engine.backend.call_tool(
            ToolDef("hvs.mystery.beep", "not registered in HA", NoArgs, ("mystery", "beep")),
            ["light.living_room_main"],
            {"entity_ids": ["light.living_room_main"]},
        )
    assert hass.services.calls == []


def test_backend_failure_after_send_blocks_retry(ha_engine):
    """HA 报错时结果未知：引擎按“已送达”处理，不去重复下发。"""
    hass, engine = ha_engine

    class BoomParser:
        def parse(self, utterance: str) -> Intent:
            return Intent(
                utterance=utterance,
                steps=[PlannedCall(tool="hvs.switch.turn_on", target="卧室加湿器")],
                provider="fixed",
            )

    engine.parser = BoomParser()
    first = engine.handle("打开加湿器")
    assert first.kind is DecisionKind.PARTIAL
    assert len(hass.services.calls) == 1
    second = engine.handle("打开加湿器")
    # P0-5：unknown 结果显式表达为 in_flight_unknown（不再是“窗口内重复”），
    # 但绝不允许第二次下发。
    assert second.kind is DecisionKind.PARTIAL, second.message
    assert second.outcomes[0].state == "in_flight_unknown"
    assert len(hass.services.calls) == 1, "结果未知的指令被再次下发"


def test_dispatch_request_service_contract(ha_engine):
    hass, engine = ha_engine
    parked = integration_services.dispatch_request(
        engine, SERVICE_HANDLE_UTTERANCE, {"text": "撤防", "session_id": "客厅"}
    )
    assert parked["kind"] == "awaiting_confirmation"
    assert parked["ok"] is True
    assert parked["dispatch_count"] == 0
    assert parked["confirmation"]["session_id"] == "客厅"
    assert parked["confirmation"]["spoken_message"] == parked["spoken_message"]

    listed = integration_services.dispatch_request(engine, SERVICE_LIST_PENDING, {"session_id": "客厅"})
    assert listed["ok"] is True and len(listed["pending"]) == 1
    assert listed["pending"][0]["risk_class"] == "alarm_disarm"
    assert listed["pending"][0]["expires_in_seconds"] > 0
    assert listed["pending"][0]["trace_id"] == parked["trace_id"]

    done = integration_services.dispatch_request(
        engine, SERVICE_CONFIRM_ACTION, {"token": parked["confirmation_token"], "session_id": "客厅"}
    )
    assert done["kind"] == "executed"
    assert len(hass.services.calls) == 1

    unknown = integration_services.dispatch_request(engine, "nope", {})
    assert unknown["ok"] is False and "handle_utterance" in unknown["message"]


def test_partial_dispatch_is_reported_as_accepted(ha_engine):
    """P0-4：下发到达设备后失败是 partial，不是被拒绝。"""
    hass, engine = ha_engine

    class BoomParser:
        def parse(self, utterance: str) -> Intent:
            return Intent(
                utterance=utterance,
                steps=[PlannedCall(tool="hvs.switch.turn_on", target="卧室加湿器")],
                provider="fixed",
            )

    engine.parser = BoomParser()
    result = integration_services.dispatch_request(engine, SERVICE_HANDLE_UTTERANCE, {"text": "打开加湿器"})
    assert result["kind"] == "partial"
    assert result["partial"] is True
    assert result["ok"] is True, "partial 不能被报成失败"
    assert result["dispatch_count"] == 0


def test_list_pending_aggregates_every_session_by_default(ha_engine):
    _hass, engine = ha_engine
    engine.handle("解锁入户门锁", session_id="客厅")
    engine.handle("解锁入户门锁", session_id="卧室")

    aggregated = integration_services.dispatch_request(engine, SERVICE_LIST_PENDING, {})
    assert aggregated["ok"] is True
    assert {item["session_id"] for item in aggregated["pending"]} == {"客厅", "卧室"}

    filtered = integration_services.dispatch_request(engine, SERVICE_LIST_PENDING, {"session_id": "客厅"})
    assert [item["session_id"] for item in filtered["pending"]] == ["客厅"]


def test_confirmation_required_event_and_notify_carry_the_token(ha_engine):
    hass, engine = ha_engine
    integration_services.async_register(hass, engine, notify_service="mobile_app_test")

    handler = hass.services.registered[(DOMAIN, SERVICE_HANDLE_UTTERANCE)]
    result = asyncio.run(handler(FakeCall({"text": "解锁入户门锁", "session_id": "客厅"})))
    token = result["confirmation_token"]
    assert token

    required = [event for event in hass.bus.events if event["event_type"] == EVENT_CONFIRMATION_REQUIRED]
    assert required, "危险动作必须触发 confirmation_required 事件"
    assert required[-1]["token"] == token
    assert required[-1]["session_id"] == "客厅"
    assert required[-1]["expires_at"] > 0

    notify_calls = [call for call in hass.services.calls if call["service"] == "send_message"]
    assert notify_calls, "配置 notify_service 后应发送确认通知"
    notify = notify_calls[-1]["data"]
    assert notify["entity_id"] == "notify.mobile_app_test"
    actions = notify["data"]["actions"]
    assert any(action["action"].startswith(f"HVS_CONFIRM:{token}") for action in actions)
    assert any(action["action"].startswith(f"HVS_CANCEL:{token}") for action in actions)
    assert token not in result["spoken_message"], "token 绝不能进 spoken_message"
    assert token not in notify["message"], "token 只能出现在自动化 action 里"


def test_cancel_service_dispatches_nothing(ha_engine):
    hass, engine = ha_engine
    parked = integration_services.dispatch_request(engine, SERVICE_HANDLE_UTTERANCE, {"text": "撤防"})
    result = integration_services.dispatch_request(
        engine, SERVICE_CANCEL_ACTION, {"token": parked["confirmation_token"]}
    )
    assert result["kind"] == "cancelled"
    assert hass.services.calls == []


def test_service_registration_and_event_flow(ha_engine):
    hass, engine = ha_engine
    integration_services.async_register(hass, engine)
    assert set(hass.services.registered) == {
        (DOMAIN, name) for name in integration_services.SERVICES
    }
    assert all(
        getattr(options["supports_response"], "value", options["supports_response"]) == "optional"
        for options in hass.services.registration_options.values()
    ), "所有服务都必须声明 supports_response=OPTIONAL，否则 HA 会丢弃返回值"

    handler = hass.services.registered[(DOMAIN, SERVICE_HANDLE_UTTERANCE)]
    result = asyncio.run(handler(FakeCall({"text": "打开客厅大灯"})))
    assert result["kind"] == "executed"
    assert hass.bus.events[-1]["event_type"] == f"{DOMAIN}_decision"
    assert hass.bus.events[-1]["service"] == SERVICE_HANDLE_UTTERANCE
    assert len(hass.services.calls) == 1

    dangerous = asyncio.run(handler(FakeCall({"text": "解锁入户门锁"})))
    assert dangerous["kind"] == "awaiting_confirmation"
    assert len(hass.services.calls) == 1, "危险动作在事件流里也不得提前下发"


def test_reexports_are_the_same_objects_as_the_core():
    from hvsl import gate as core_gate
    from hvsl import risk as core_risk

    assert integration_safety.ConfirmationGate is core_gate.ConfirmationGate
    assert integration_safety.RiskPolicy is core_risk.RiskPolicy
    assert integration_safety.combine is core_risk.combine


def test_services_yaml_matches_the_registered_surface():
    text = (ROOT / "custom_components" / DOMAIN / "services.yaml").read_text(encoding="utf-8")
    for name in integration_services.SERVICES:
        assert f"  {name}:" in text, f"services.yaml 缺少 {name} 的说明"
    for forbidden in ("任意品牌", "米家", "云端账号"):
        assert forbidden not in text


def test_manifest_declares_the_integration_itself():
    manifest = json.loads((ROOT / "custom_components" / DOMAIN / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["domain"] == DOMAIN
    assert manifest["config_flow"] is True
    assert manifest["version"] == "0.2.0"
    assert "requirements" in manifest


def test_const_domain_matches_the_integration_directory():
    assert (ROOT / "custom_components" / DOMAIN).is_dir()
    assert (ROOT / "custom_components" / DOMAIN / "manifest.json").is_file()
