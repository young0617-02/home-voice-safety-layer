"""Relative brightness/volume adjustment (task #43).

Every case goes through the real rule parser and the real decision engine, so what
is asserted here is the path a spoken sentence actually takes: parse -> resolve ->
policy -> backend -> audit.  The backend-level cases pin the "no current state means
no dispatch" contract, which is the whole point of replacing the old 50% guess.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import httpx
import pytest
from conftest import CATALOG, POLICY, make_engine
from pydantic import ValidationError

from custom_components.home_voice_safety.backend import HassLocalBackend

from hvsl.backends.base import resolve_adjust_args
from hvsl.backends.home_assistant import HomeAssistantBackend
from hvsl.backends.simulated import SimulatedBackend
from hvsl.errors import PreDispatchError, ToolValidationError
from hvsl.models import DecisionKind, DeviceEntity
from hvsl.risk import RiskPolicy
from hvsl.tools import DeltaArgs, build_default_registry
from pipeline.nlu import RuleParser


def _registry():
    return build_default_registry()


def _engine(backend, *, policy: RiskPolicy | None = None):
    registry = _registry()
    return make_engine(
        backend=backend,
        parser=RuleParser(registry),
        registry=registry,
        policy=policy,
    )


def _light_backend(*, state: str = "on", brightness: int | None = 102) -> SimulatedBackend:
    """Bedroom light from the reviewed catalog; brightness 102 on the 0-255 scale = 40%."""
    backend = SimulatedBackend.from_catalog(CATALOG)
    entity = backend.get("light.bedroom_main")
    assert entity is not None
    entity.state = state
    if brightness is None:
        entity.attributes.pop("brightness", None)
    else:
        entity.attributes["brightness"] = brightness
    return backend


def _media_backend(*, state: str = "on", volume_level: float | None = 0.2) -> SimulatedBackend:
    """Living-room TV; volume_level is 0.0-1.0 in both simulated and HA."""
    backend = SimulatedBackend.from_catalog(CATALOG)
    entity = backend.get("media_player.living_room_tv")
    assert entity is not None
    entity.state = state
    if volume_level is None:
        entity.attributes.pop("volume_level", None)
    else:
        entity.attributes["volume_level"] = volume_level
    return backend


def _user_selected_policy() -> RiskPolicy:
    """Same declarative overlay the config wizard writes for user-selected devices."""
    payload = RiskPolicy.load(POLICY).to_dict()
    payload["rules"].append(
        {
            "id": "user_selected_device",
            "risk": "confirm",
            "match": {"tool_prefix_any": ["hvs."], "tag_any": ["danger:user_selected"]},
            "reason": "用户标记的危险设备，动作必须二次确认。",
            "prompt": "该指令会操作【{{targets}}】，请回复确认（confirm）后才会下发。",
        }
    )
    return RiskPolicy.from_dict(payload)


def test_light_on_brighten_uses_the_current_brightness():
    backend = _light_backend(state="on", brightness=102)  # 40%
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调亮一点")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.dispatch_count == 1
    entity = backend.get("light.bedroom_main")
    assert entity is not None
    assert entity.state == "on"
    assert entity.attributes["brightness"] == round(50 * 2.54)  # 50%
    assert backend.dispatches[-1]["args"]["brightness_pct"] == 50
    assert "delta_pct" not in backend.dispatches[-1]["args"]
    engine.store.close()


def test_light_on_dim_uses_the_current_brightness():
    backend = _light_backend(state="on", brightness=102)  # 40%
    engine = _engine(backend)
    decision = engine.handle("把卧室的灯调暗一点")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert backend.dispatches[-1]["args"]["brightness_pct"] == 30
    entity = backend.get("light.bedroom_main")
    assert entity is not None and entity.attributes["brightness"] == round(30 * 2.54)
    engine.store.close()


def test_light_off_brighten_starts_from_zero_and_turns_the_light_on():
    backend = _light_backend(state="off", brightness=None)
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调亮一点")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.dispatch_count == 1
    entity = backend.get("light.bedroom_main")
    assert entity is not None
    assert entity.state == "on"
    assert backend.dispatches[-1]["args"]["brightness_pct"] == 10
    assert entity.attributes["brightness"] == round(10 * 2.54)
    engine.store.close()


def test_light_off_dim_is_refused_with_zero_dispatches():
    backend = _light_backend(state="off", brightness=None)
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调暗一点")

    assert decision.kind is not DecisionKind.EXECUTED
    assert decision.dispatch_count == 0
    assert backend.dispatches == []
    assert decision.outcomes and decision.outcomes[0].dispatched is False
    assert decision.outcomes[0].state == "failed"
    assert "已关闭" in decision.message
    engine.store.close()


def test_light_on_without_brightness_is_refused():
    backend = _light_backend(state="on", brightness=None)
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调亮一点")

    assert decision.kind is not DecisionKind.EXECUTED
    assert decision.dispatch_count == 0
    assert backend.dispatches == []
    assert "读不到" in decision.message and "亮度" in decision.message
    engine.store.close()


def test_light_unavailable_state_is_refused():
    backend = _light_backend(state="unavailable", brightness=102)
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调亮一点")

    assert decision.kind is not DecisionKind.EXECUTED
    assert decision.dispatch_count == 0
    assert backend.dispatches == []
    assert "unavailable" in decision.message
    engine.store.close()


def test_media_volume_relative_up_uses_current_volume():
    backend = _media_backend(state="on", volume_level=0.2)  # 20%
    engine = _engine(backend)
    decision = engine.handle("声音大一点")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.steps[0].tool == "hvs.media_player.adjust_volume"
    assert backend.dispatches[-1]["args"]["volume_level"] == 30
    entity = backend.get("media_player.living_room_tv")
    assert entity is not None and entity.attributes["volume_level"] == 0.3
    engine.store.close()


@pytest.mark.parametrize("state", ["playing", "paused", "idle"])
def test_media_player_playback_states_still_allow_relative_volume(state: str):
    """真实 HA 媒体实体返回 playing/paused/idle，而不是 on/off。"""
    backend = _media_backend(state=state, volume_level=0.4)
    engine = _engine(backend)
    decision = engine.handle("声音大一点")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.steps[0].tool == "hvs.media_player.adjust_volume"
    assert backend.dispatches[-1]["args"]["volume_level"] == 50
    engine.store.close()


def test_media_player_off_with_readable_volume_still_adjusts():
    """音量是设备属性，与播放状态解耦：off 但音量可读时仍可调。"""
    backend = _media_backend(state="off", volume_level=0.4)
    engine = _engine(backend)
    decision = engine.handle("声音大一点")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert backend.dispatches[-1]["args"]["volume_level"] == 50
    engine.store.close()


def test_media_player_without_volume_is_refused_with_zero_dispatches():
    backend = _media_backend(state="playing", volume_level=None)
    engine = _engine(backend)
    decision = engine.handle("声音大一点")

    assert decision.dispatch_count == 0
    assert "未下发" in decision.spoken_message
    assert "可重试" not in decision.spoken_message
    engine.store.close()


def test_absolute_volume_phrase_keeps_the_existing_set_behaviour():
    backend = _media_backend(state="on", volume_level=0.2)
    engine = _engine(backend)
    decision = engine.handle("电视音量调到35")

    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.steps[0].tool == "hvs.media_player.volume_set"
    assert backend.dispatches[-1]["args"]["volume_level"] == 35
    entity = backend.get("media_player.living_room_tv")
    assert entity is not None and entity.attributes["volume_level"] == 0.35
    engine.store.close()


def test_open_and_off_phrases_are_not_parsed_as_relative_adjustment():
    parser = RuleParser(_registry())
    opened = parser.parse("打开卧室的灯").steps
    turned_off = parser.parse("把卧室的灯关掉").steps

    assert [step.tool for step in opened] == ["hvs.light.turn_on"]
    assert [step.tool for step in turned_off] == ["hvs.light.turn_off"]
    assert "delta_pct" not in opened[0].args
    assert "delta_pct" not in turned_off[0].args


def test_danger_tagged_light_still_requires_confirmation_before_any_dispatch():
    backend = _light_backend(state="on", brightness=102)
    entity = backend.get("light.bedroom_main")
    assert entity is not None
    entity.tags.append("danger:user_selected")
    engine = _engine(backend, policy=_user_selected_policy())

    parked = engine.handle("卧室的灯调亮一点")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION, parked.message
    assert parked.dispatch_count == 0
    assert backend.dispatches == []

    confirmed = engine.confirm(parked.confirmation_token)
    assert confirmed.kind is DecisionKind.EXECUTED, confirmed.message
    assert confirmed.dispatch_count == 1
    assert backend.dispatches[-1]["args"]["brightness_pct"] == 50
    engine.store.close()


def test_delta_bounds_are_enforced_by_the_registry():
    registry = _registry()
    assert registry.validate_args(
        "hvs.light.adjust_brightness", {"entity_ids": ["light.x"], "delta_pct": 50}
    )["delta_pct"] == 50
    assert registry.validate_args(
        "hvs.media_player.adjust_volume", {"entity_ids": ["media_player.x"], "delta_pct": -50}
    )["delta_pct"] == -50

    for delta in (51, -51):
        with pytest.raises(ToolValidationError):
            registry.validate_args(
                "hvs.light.adjust_brightness", {"entity_ids": ["light.x"], "delta_pct": delta}
            )
    with pytest.raises(ToolValidationError):
        registry.validate_args("hvs.light.adjust_brightness", {"entity_ids": ["light.x"]})


def test_clamping_never_leaves_the_percentage_range():
    light = DeviceEntity(
        entity_id="light.x",
        domain="light",
        display_name="测试灯",
        state="on",
        attributes={"brightness": 242},  # ~95%
    )
    tv = DeviceEntity(
        entity_id="media_player.x",
        domain="media_player",
        display_name="测试电视",
        state="on",
        attributes={"volume_level": 0.05},  # 5%
    )

    assert resolve_adjust_args(light, 10) == {"brightness_pct": 100}
    assert resolve_adjust_args(tv, -10) == {"volume_level": 0}


def _ha_backend(states: list[dict], posts: list[dict]) -> HomeAssistantBackend:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/states":
            return httpx.Response(200, json=states)
        if request.method == "POST" and request.url.path.startswith("/api/services/"):
            posts.append(json.loads(request.content))
            return httpx.Response(200, json=[])
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=2.0)
    return HomeAssistantBackend(
        "http://ha.invalid", "token", catalog_path=CATALOG, client=client
    )


def test_ha_backend_reads_states_and_posts_the_absolute_brightness():
    posts: list[dict] = []
    backend = _ha_backend(
        [
            {
                "entity_id": "light.bedroom_main",
                "state": "on",
                "attributes": {"brightness": 102, "friendly_name": "卧室主灯"},
            }
        ],
        posts,
    )
    result = backend.call_tool(
        _registry().get("hvs.light.adjust_brightness"),
        ["light.bedroom_main"],
        {"entity_ids": ["light.bedroom_main"], "delta_pct": 10},
    )

    assert result["ok"] is True
    assert len(posts) == 1
    assert posts[0]["brightness"] == round(50 * 2.54)  # HA wire shape for 50%
    assert backend.dispatches[-1]["args"]["brightness"] == round(50 * 2.54)
    backend.close()


def test_ha_backend_refuses_unavailable_state_and_posts_nothing():
    posts: list[dict] = []
    backend = _ha_backend(
        [
            {
                "entity_id": "light.bedroom_main",
                "state": "unavailable",
                "attributes": {"brightness": 102, "friendly_name": "卧室主灯"},
            }
        ],
        posts,
    )

    with pytest.raises(PreDispatchError, match="unavailable"):
        backend.call_tool(
            _registry().get("hvs.light.adjust_brightness"),
            ["light.bedroom_main"],
            {"entity_ids": ["light.bedroom_main"], "delta_pct": 10},
        )
    assert posts == []
    backend.close()


def test_ha_backend_refuses_on_without_brightness_and_posts_nothing():
    posts: list[dict] = []
    backend = _ha_backend(
        [
            {
                "entity_id": "light.bedroom_main",
                "state": "on",
                "attributes": {"friendly_name": "卧室主灯"},
            }
        ],
        posts,
    )

    with pytest.raises(PreDispatchError, match="亮度"):
        backend.call_tool(
            _registry().get("hvs.light.adjust_brightness"),
            ["light.bedroom_main"],
            {"entity_ids": ["light.bedroom_main"], "delta_pct": 10},
        )
    assert posts == []
    backend.close()


def test_delta_args_model_still_rejects_zero_width_out_of_range_values():
    """Guard against a future refactor widening the ±50 contract."""
    assert DeltaArgs(entity_ids=["light.x"], delta_pct=50).delta_pct == 50
    with pytest.raises(ValidationError):
        DeltaArgs(entity_ids=["light.x"], delta_pct=51)

class _LocalState:
    def __init__(self, entity_id: str, state: str, attributes: dict[str, Any]) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes


class _LocalStates:
    def __init__(self, state: _LocalState) -> None:
        self._state = state

    def async_all(self) -> list[_LocalState]:
        return [self._state]

    def get(self, entity_id: str) -> _LocalState | None:
        return self._state if entity_id == self._state.entity_id else None


class _LocalServices:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) == ("light", "turn_on")

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        blocking: bool = False,
        **kwargs: Any,
    ) -> None:
        self.calls.append({"domain": domain, "service": service, "data": dict(data or {}), "blocking": blocking})


class _LocalHass:
    def __init__(self, loop: asyncio.AbstractEventLoop, services: _LocalServices, state: _LocalState) -> None:
        self.loop = loop
        self.services = services
        self.states = _LocalStates(state)


@pytest.fixture(scope="module")
def ha_local_loop():
    """HA 的事件循环跑在后台线程；backend 从同步 executor 线程桥接过去。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ha_local_backend_reads_live_state_and_dispatches_absolute(ha_local_loop):
    services = _LocalServices()
    state = _LocalState("light.bedroom_main", "on", {"brightness": 102, "friendly_name": "卧室主灯"})
    backend = HassLocalBackend(_LocalHass(ha_local_loop, services, state), catalog_path=CATALOG)

    result = backend.call_tool(
        _registry().get("hvs.light.adjust_brightness"),
        ["light.bedroom_main"],
        {"entity_ids": ["light.bedroom_main"], "delta_pct": 10},
    )

    assert result["ok"] is True
    assert services.calls[-1]["data"] == {"brightness": round(50 * 2.54), "entity_id": "light.bedroom_main"}
    assert services.calls[-1]["blocking"] is True
    assert backend.dispatches[-1]["payload"]["brightness"] == round(50 * 2.54)


def test_ha_local_backend_refuses_unavailable_state_before_the_bus(ha_local_loop):
    services = _LocalServices()
    state = _LocalState("light.bedroom_main", "unavailable", {"brightness": 102, "friendly_name": "卧室主灯"})
    backend = HassLocalBackend(_LocalHass(ha_local_loop, services, state), catalog_path=CATALOG)

    with pytest.raises(PreDispatchError, match="unavailable"):
        backend.call_tool(
            _registry().get("hvs.light.adjust_brightness"),
            ["light.bedroom_main"],
            {"entity_ids": ["light.bedroom_main"], "delta_pct": 10},
        )
    assert services.calls == []
    assert backend.dispatches == []


def test_repeated_dim_in_the_same_session_dispatches_each_time():
    """Saying "调暗一点" twice means two -10 steps, not a deduplicated no-op."""
    backend = _light_backend(state="on", brightness=102)  # 40%
    engine = _engine(backend)

    first = engine.handle("把卧室主灯调暗一点")
    second = engine.handle("把卧室主灯调暗一点")

    assert first.kind is DecisionKind.EXECUTED, first.message
    assert second.kind is DecisionKind.EXECUTED, second.message
    assert first.dispatch_count == 1 and second.dispatch_count == 1
    assert [item["args"]["brightness_pct"] for item in backend.dispatches] == [30, 20]
    assert backend.dispatch_count == 2
    entity = backend.get("light.bedroom_main")
    assert entity is not None
    assert entity.attributes["brightness"] == round(20 * 2.54)  # 20%
    engine.store.close()


def test_repeated_dangerous_unlock_is_still_blocked_by_the_idempotency_guard():
    """The non-idempotent exception must never leak into dangerous tools."""
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = _engine(backend)

    first = engine.handle("解锁入户门锁")
    assert first.kind is DecisionKind.AWAITING_CONFIRMATION
    assert engine.confirm(first.confirmation_token).kind is DecisionKind.EXECUTED
    assert backend.dispatch_count == 1

    second = engine.handle("解锁入户门锁")
    assert second.kind is DecisionKind.AWAITING_CONFIRMATION, "危险动作仍须二次确认"
    replayed = engine.confirm(second.confirmation_token)
    assert replayed.kind is DecisionKind.DEDUPLICATED, replayed.message
    assert replayed.dispatch_count == 0
    assert backend.dispatch_count == 1, "危险动作被重复下发"
    engine.store.close()


def test_off_dim_refusal_is_not_marked_retryable():
    backend = _light_backend(state="off", brightness=None)
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调暗一点")

    assert decision.dispatch_count == 0
    assert backend.dispatches == []
    assert "未下发" in decision.spoken_message
    assert "可重试" not in decision.spoken_message
    assert decision.outcomes[0].error.startswith("未下发：")
    engine.store.close()


def test_missing_brightness_refusal_is_not_marked_retryable():
    backend = _light_backend(state="on", brightness=None)
    engine = _engine(backend)
    decision = engine.handle("卧室的灯调亮一点")

    assert decision.dispatch_count == 0
    assert backend.dispatches == []
    assert "未下发" in decision.spoken_message
    assert "可重试" not in decision.spoken_message
    assert decision.outcomes[0].error.startswith("未下发：")
    engine.store.close()
