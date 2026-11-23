"""P0-4：sensor 的 state attributes 绝不能泄漏 pending token 或用户原话。

HA recorder 会持久化 state attributes；``pending_view`` 里的 token/utterance
只允许留在内存快照与服务返回值中，不能出现在传感器上。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
import types

TOKEN = "deadbeef99"
UTTERANCE = "解锁入户门锁"
SESSION = "客厅音箱"
TRACE = "abc123def456"
ENTRY = types.SimpleNamespace(entry_id="entry-1")


class _Generic:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls


class _SensorEntity(_Generic):
    pass


class _CoordinatorEntity(_Generic):
    def __init__(self, coordinator):
        self.coordinator = coordinator


class _DataUpdateCoordinator(_Generic):
    def __init__(self, *args, **kwargs):
        self.data = None


class _ConfigEntry(_Generic):
    pass


class _DeviceInfo:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs


def _install_ha_stubs() -> None:
    """Sensor imports HA; the test environment intentionally has no HA install."""
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

    module("homeassistant.components")
    module("homeassistant.components.sensor", SensorEntity=_SensorEntity)
    module("homeassistant.config_entries", ConfigEntry=_ConfigEntry)
    module("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}))
    module("homeassistant.helpers")
    module("homeassistant.helpers.device_registry", DeviceInfo=_DeviceInfo)
    module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
    module(
        "homeassistant.helpers.update_coordinator",
        CoordinatorEntity=_CoordinatorEntity,
        DataUpdateCoordinator=_DataUpdateCoordinator,
        UpdateFailed=type("UpdateFailed", (Exception,), {}),
    )


_install_ha_stubs()
sensor = importlib.import_module("custom_components.home_voice_safety.sensor")


class FakeCoordinator:
    def __init__(self, data: dict) -> None:
        self.data = data


def _coordinator() -> FakeCoordinator:
    return FakeCoordinator(
        {
            "entities": 4,
            "real_devices": 1,
            "simulated_devices": 3,
            "dangerous_entities": 2,
            "pending": [
                {
                    "token": TOKEN,
                    "utterance": UTTERANCE,
                    "session_id": SESSION,
                    "risk_class": "door_unlock",
                    "targets": ["lock.front_door"],
                    "tools": ["hvs.lock.unlock"],
                    "trace_id": TRACE,
                    "expires_at": 2000.0,
                    "expires_in_seconds": 42.0,
                }
            ],
            "stats": {"dispatched": 2, "suppressed": 0, "failed": 0, "pending": 1},
        }
    )


def _sensors() -> list:
    coordinator = _coordinator()
    return [
        sensor.PendingActionsSensor(coordinator, ENTRY),
        sensor.DeviceInventorySensor(coordinator, ENTRY),
        sensor.DecisionStatsSensor(coordinator, ENTRY),
    ]


def test_no_sensor_attributes_contain_token_or_utterance():
    for entity in _sensors():
        blob = json.dumps(entity.extra_state_attributes, ensure_ascii=False)
        assert TOKEN not in blob, f"{type(entity).__name__} 泄漏 token"
        assert UTTERANCE not in blob, f"{type(entity).__name__} 泄漏用户原话"
        assert '"token"' not in blob
        assert '"utterance"' not in blob


def test_pending_sensor_actions_are_unrecorded():
    entity = _sensors()[0]
    assert entity._unrecorded_attributes == frozenset({"actions"})


def test_pending_sensor_exposes_only_safe_summary_fields():
    entity = _sensors()[0]
    assert entity.native_value == 1
    attributes = entity.extra_state_attributes
    assert attributes["count"] == 1
    action = attributes["actions"][0]
    assert set(action) == {"risk_class", "session_alias", "expires_in", "trace_id_hash"}
    assert action["risk_class"] == "door_unlock"
    assert action["expires_in"] == 42.0
    assert action["session_alias"] == hashlib.sha256(SESSION.encode("utf-8")).hexdigest()[:8]
    assert action["trace_id_hash"] == hashlib.sha256(TRACE.encode("utf-8")).hexdigest()[:8]


def test_inventory_and_stats_sensors_are_also_clean():
    _pending, inventory, stats = _sensors()
    assert inventory.extra_state_attributes["实体总数"] == 4
    assert stats.extra_state_attributes["dispatched"] == 2
    for entity in (inventory, stats):
        blob = json.dumps(entity.extra_state_attributes, ensure_ascii=False)
        assert TOKEN not in blob and UTTERANCE not in blob