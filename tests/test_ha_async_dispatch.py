"""F2：HA 真机下发必须走 ``async_call`` 的事件循环桥接。

现代 Home Assistant 不再提供公开的同步 ``hass.services.call(...)``；引擎跑在
executor 线程，只能通过::

    asyncio.run_coroutine_threadsafe(
        hass.services.async_call(domain, service, data, blocking=True), hass.loop
    )

把命令送回事件循环。本文件的 ``FakeServices`` **只实现 ``async_call``**（没有
``call``），因此一旦集成回退到同步 API，测试会立刻以 AttributeError 暴露缺口。

失败映射也在真机路径上验证：

* ``ServiceNotFound``（服务不存在）-> ``PreDispatchError``：指令未发出，可重试；
* future 超时 -> ``DispatchTimeoutError``：结果未知，幂等守卫不得重发。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import FixedParser

from custom_components.home_voice_safety.backend import HassLocalBackend
from hvsl.errors import DispatchTimeoutError, PreDispatchError
from hvsl.models import DecisionKind, PlannedCall
from hvsl.tools import build_default_registry

ROOT = Path(__file__).resolve().parents[1]
CATALOG = str(ROOT / "devices" / "devices.json")
POLICY = str(ROOT / "hvsl" / "policy" / "risk_policy.json")

HA_STATES = [
    ("light.living_room_main", "off", {"friendly_name": "客厅大灯"}),
    ("switch.gas_valve", "off", {"friendly_name": "燃气阀"}),
    ("lock.front_door", "locked", {"friendly_name": "入户门锁"}),
    ("switch.humidifier", "off", {"friendly_name": "卧室加湿器"}),
]


class ServiceNotFound(Exception):
    """本地替身：真实类来自 ``homeassistant.exceptions``，backend 按类名识别。"""


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


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def async_fire(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append({"event_type": name, **payload})


class FakeServices:
    """只暴露 ``async_call``：刻意不实现 ``call``（现代 HA 的真实形态）。"""

    def __init__(self, *, delay: float = 0.0, service_not_found: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.delay = delay
        self.service_not_found = service_not_found
        self.known = {tool.ha_service for tool in build_default_registry().all()}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.known

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
        blocking: bool = False,
        **kwargs: Any,
    ) -> None:
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "data": dict(data or {}),
                "blocking": blocking,
            }
        )
        if self.service_not_found:
            raise ServiceNotFound(f"{domain}.{service}")
        if self.delay:
            await asyncio.sleep(self.delay)


class FakeHass:
    def __init__(self, loop: asyncio.AbstractEventLoop, services: FakeServices) -> None:
        self.loop = loop
        self.services = services
        self.states = FakeStates([FakeState(*row) for row in HA_STATES])
        self.bus = FakeBus()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture(scope="module")
def ha_loop():
    """HA 事件循环跑在后台线程；同步 executor 线程通过桥接提交协程。"""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def _make_engine(
    loop: asyncio.AbstractEventLoop,
    services: FakeServices,
    planned: PlannedCall,
    *,
    timeout: float = 10.0,
):
    from hvsl.config import Settings
    from hvsl.engine import DecisionEngine
    from hvsl.risk import RiskPolicy
    from hvsl.store import Store

    hass = FakeHass(loop, services)
    backend = HassLocalBackend(hass, catalog_path=CATALOG, timeout=timeout)
    registry = build_default_registry()
    settings = Settings(
        db_path=":memory:",
        policy_path=POLICY,
        device_catalog=CATALOG,
        confirm_ttl_seconds=60.0,
        dedup_window_seconds=30.0,
    )
    engine = DecisionEngine(
        backend,
        FixedParser([planned]),
        settings=settings,
        registry=registry,
        policy=RiskPolicy.load(POLICY),
        store=Store(":memory:"),
    )
    return engine, backend


def test_unlock_is_dispatched_via_async_call_exactly_once(ha_loop):
    services = FakeServices()
    engine, backend = _make_engine(
        ha_loop,
        services,
        PlannedCall(tool="hvs.lock.unlock", target="入户门锁"),
    )
    assert not hasattr(FakeServices, "call"), "FakeServices 不得再提供同步 call，否则会遮住真机缺口"

    parked = engine.handle("解锁入户门锁")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION, parked.message
    assert services.calls == [], "确认前不得调用 HA 服务"

    done = engine.confirm(parked.confirmation_token)
    assert done.kind is DecisionKind.EXECUTED, done.message
    assert len(services.calls) == 1, "一次确认只允许下发一次"
    call = services.calls[0]
    assert (call["domain"], call["service"]) == ("lock", "unlock")
    assert call["data"] == {"entity_id": "lock.front_door"}
    assert call["blocking"] is True
    assert backend.dispatches == [
        {"tool": "hvs.lock.unlock", "entity_ids": ["lock.front_door"], "payload": {"entity_id": "lock.front_door"}}
    ]
    engine.store.close()


def test_service_not_found_maps_to_predispatch_error(ha_loop):
    services = FakeServices(service_not_found=True)
    hass = FakeHass(ha_loop, services)
    backend = HassLocalBackend(hass, catalog_path=CATALOG)

    with pytest.raises(PreDispatchError, match=r"lock\.unlock"):
        backend.call_tool(
            build_default_registry().get("hvs.lock.unlock"),
            ["lock.front_door"],
            {"entity_ids": ["lock.front_door"]},
        )
    assert backend.dispatches == [], "未发出的指令不得记入 dispatch 台账"


def test_timeout_maps_to_dispatch_timeout_directly(ha_loop):
    """超时的直接映射：``concurrent.futures.TimeoutError`` -> ``DispatchTimeoutError``。"""
    services = FakeServices(delay=0.2)
    hass = FakeHass(ha_loop, services)
    backend = HassLocalBackend(hass, catalog_path=CATALOG, timeout=0.02)

    with pytest.raises(DispatchTimeoutError, match="超时"):
        backend.call_tool(
            build_default_registry().get("hvs.lock.unlock"),
            ["lock.front_door"],
            {"entity_ids": ["lock.front_door"]},
        )
    assert backend.dispatches == []
    time.sleep(0.25)


def test_timeout_maps_to_dispatch_timeout_and_is_never_resent(ha_loop):
    services = FakeServices(delay=0.2)
    engine, backend = _make_engine(
        ha_loop,
        services,
        PlannedCall(tool="hvs.switch.turn_on", target="卧室加湿器"),
        timeout=0.05,
    )

    first = engine.handle("打开加湿器")
    assert first.kind is DecisionKind.PARTIAL, first.message
    assert first.outcomes[0].state == "failed"
    assert first.outcomes[0].dispatched is True, "超时的语义是“可能已送达”，不是未送达"
    assert backend.dispatches == []
    assert len(services.calls) == 1

    second = engine.handle("打开加湿器")
    # P0-5：超时后的 unknown 不再伪装成“窗口内重复”，而是显式 in_flight_unknown；
    # 两种表达都绝不重发。
    assert second.kind is DecisionKind.PARTIAL, second.message
    assert second.outcomes[0].state == "in_flight_unknown"
    assert len(services.calls) == 1, "超时后结果未知，绝不允许重发"
    engine.store.close()
    time.sleep(0.25)  # 让后台 loop 里被超时的协程收尾，避免关闭 loop 时出现 pending task


def test_unregistered_service_is_refused_before_the_loop(ha_loop):
    services = FakeServices()
    hass = FakeHass(ha_loop, services)
    backend = HassLocalBackend(hass, catalog_path=CATALOG)
    registry = build_default_registry()


    from hvsl.tools import NoArgs, ToolDef

    tool = ToolDef("hvs.mystery.beep", "unknown service", NoArgs, ("mystery", "beep"))
    assert registry.has("hvs.mystery.beep") is False
    with pytest.raises(PreDispatchError):
        backend.call_tool(tool, ["light.living_room_main"], {"entity_ids": ["light.living_room_main"]})
    assert services.calls == []
    assert backend.dispatches == []