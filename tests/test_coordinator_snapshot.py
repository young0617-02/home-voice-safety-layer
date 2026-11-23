"""P1-1：coordinator 快照性能 —— 实体缓存、事件驱动、stats 聚合 SQL。"""

from __future__ import annotations

import asyncio
import importlib
import sys
import time
import types

from conftest import FixedParser, make_engine, step
from hvsl.models import DeviceEntity, Provenance


class _Generic:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls


class _ConfigEntry(_Generic):
    pass


class _HomeAssistant(_Generic):
    pass


class _DataUpdateCoordinator(_Generic):
    def __init__(self, *args, **kwargs):
        self.data = None

    def async_set_updated_data(self, data):
        self.data = data


class _UpdateFailed(Exception):
    pass


def _install_ha_stubs() -> None:
    if "homeassistant" not in sys.modules:
        sys.modules["homeassistant"] = types.ModuleType("homeassistant")

    def module(name: str, **attrs: object) -> types.ModuleType:
        target = sys.modules.get(name) or types.ModuleType(name)
        for key, value in attrs.items():
            setattr(target, key, value)
        sys.modules[name] = target
        parent, _, child = name.rpartition(".")
        if parent:
            setattr(sys.modules[parent], child, target)
        return target

    module("homeassistant.config_entries", ConfigEntry=_ConfigEntry)
    module("homeassistant.core", HomeAssistant=_HomeAssistant)
    module("homeassistant.helpers")
    module(
        "homeassistant.helpers.update_coordinator",
        DataUpdateCoordinator=_DataUpdateCoordinator,
        UpdateFailed=_UpdateFailed,
        CoordinatorEntity=_Generic,
    )


_install_ha_stubs()
coordinator_mod = importlib.import_module("custom_components.home_voice_safety.coordinator")

ENTITIES = [
    DeviceEntity(entity_id="light.living_room_main", domain="light", display_name="客厅大灯",
                 provenance=Provenance.REAL),
    DeviceEntity(entity_id="switch.gas_valve", domain="switch", display_name="燃气阀",
                 provenance=Provenance.SIMULATED, tags=["danger:gas"]),
]


class FakeBackend:
    def __init__(self) -> None:
        self.calls = 0

    def entities(self) -> list[DeviceEntity]:
        self.calls += 1
        return list(ENTITIES)


class FakeGate:
    def all(self, *, now: float | None = None):
        return []


class FakeEngine:
    def __init__(self) -> None:
        self.backend = FakeBackend()
        self.gate = FakeGate()

    def stats(self) -> dict[str, int]:
        return {"dispatched": 0, "suppressed": 0, "failed": 0, "pending": 0}


class FakeHass:
    def __init__(self) -> None:
        self.config = types.SimpleNamespace(config_dir=".")
        self.tasks: list = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_task(self, coro):
        self.tasks.append(coro)
        coro.close()


def _coordinator(engine: FakeEngine):
    coordinator = object.__new__(coordinator_mod.HomeVoiceSafetyCoordinator)
    coordinator.engine = engine
    coordinator.hass = FakeHass()
    coordinator._entities_cache = None
    coordinator._entities_cached_at = 0.0
    coordinator._refresh_pending = False
    return coordinator


def test_poll_interval_is_only_a_fallback():
    assert coordinator_mod.POLL_INTERVAL_SECONDS == 300
    assert coordinator_mod.ENTITIES_CACHE_SECONDS == 30.0


def test_snapshot_reads_entities_once_per_cache_window():
    engine = FakeEngine()
    coordinator = _coordinator(engine)

    first = coordinator.snapshot()
    second = coordinator.snapshot()
    assert first == second
    assert engine.backend.calls == 1, "一次 snapshot 及其缓存窗口内只能调用一次 entities()"
    assert first["entities"] == 2
    assert first["real_devices"] == 1
    assert first["simulated_devices"] == 1
    assert first["dangerous_entities"] == 1

    coordinator._entities_cached_at = time.time() - coordinator_mod.ENTITIES_CACHE_SECONDS - 1
    coordinator.snapshot()
    assert engine.backend.calls == 2, "缓存过期后必须重新读取实体"


def test_decision_event_forces_a_fresh_snapshot_and_publishes_it():
    engine = FakeEngine()
    coordinator = _coordinator(engine)
    coordinator._entities_cache = []
    coordinator._entities_cached_at = time.time()  # 缓存仍有效

    asyncio.run(coordinator.async_refresh_from_event())

    assert engine.backend.calls == 1, "事件驱动这一轮必须绕过缓存读一次真实实体"
    assert coordinator.data is not None
    assert coordinator.data["entities"] == 2
    assert coordinator._refresh_pending is False


def test_same_turn_events_are_coalesced():
    engine = FakeEngine()
    coordinator = _coordinator(engine)
    coordinator._handle_decision_event(object())
    coordinator._handle_decision_event(object())
    assert len(coordinator.hass.tasks) == 1, "同一轮多个服务事件只应调度一次刷新"


def test_engine_stats_uses_aggregate_sql_not_the_whole_table():
    engine = make_engine(parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]))
    engine.handle("打开客厅大灯")

    calls: list[str] = []
    original = engine.store.query

    def counting_query(sql: str, params: tuple = ()):
        calls.append(sql)
        return original(sql, params)

    engine.store.query = counting_query  # type: ignore[method-assign]
    stats = engine.stats()

    assert set(stats) == {"dispatched", "suppressed", "failed", "pending"}
    assert stats["dispatched"] == 1
    assert len(calls) <= 4, calls
    assert any("GROUP BY" in sql for sql in calls)
    assert not any("SELECT * FROM audit" in sql for sql in calls), "stats 不得拉全表"
    engine.store.close()