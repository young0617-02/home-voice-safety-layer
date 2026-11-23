"""Read-only sensors that make the safety layer visible in the HA UI (F-003/F-009)."""

from __future__ import annotations

import hashlib
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, INTEGRATION_VERSION
from .coordinator import HomeVoiceSafetyCoordinator


def _short_hash(value: str) -> str:
    """Stable, non-reversible fingerprint (never expose the raw id in state)."""
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:8]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HomeVoiceSafetyCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            PendingActionsSensor(coordinator, entry),
            DeviceInventorySensor(coordinator, entry),
            DecisionStatsSensor(coordinator, entry),
        ]
    )


class _Base(CoordinatorEntity[HomeVoiceSafetyCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: HomeVoiceSafetyCoordinator, entry: ConfigEntry, key: str, name: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_name = name
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Home Voice Safety Layer",
            manufacturer="Home Voice Safety",
            model="Decision & Safety Layer",
            sw_version=INTEGRATION_VERSION,
        )


class PendingActionsSensor(_Base):
    """How many dangerous actions are waiting for a confirmation right now.

    P0-4 privacy: HA recorder persists state attributes.  A pending action holds a
    raw token and the user's sentence, so we expose neither.  ``actions`` (the only
    per-action payload) is declared unrecorded; it carries a risk class, a hashed
    session alias, remaining seconds and a hashed trace id.
    """

    _unrecorded_attributes = frozenset({"actions"})

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "pending_actions", "待确认动作")
        self._attr_icon = "mdi:lock-alert"

    @property
    def native_value(self) -> int:
        return len(self._data.get("pending", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        actions = [
            {
                "risk_class": item.get("risk_class", ""),
                "session_alias": _short_hash(str(item.get("session_id", ""))),
                "expires_in": item.get("expires_in_seconds", 0),
                "trace_id_hash": _short_hash(str(item.get("trace_id", ""))),
            }
            for item in self._data.get("pending", [])
        ]
        # count 是安全的聚合值；逐条明细在 actions 里，且不会进入 recorder。
        return {"count": len(actions), "actions": actions}

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {"pending": []}


class DeviceInventorySensor(_Base):
    """Real vs simulated device counts, straight from the reviewed catalog (A6)."""

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "device_inventory", "受控设备清点")
        self._attr_icon = "mdi:home-automation"

    @property
    def native_value(self) -> int:
        return int(self._data.get("real_devices", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "真实设备": self._data.get("real_devices", 0),
            "模拟实体": self._data.get("simulated_devices", 0),
            "带危险标签": self._data.get("dangerous_entities", 0),
            "实体总数": self._data.get("entities", 0),
        }

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}


class DecisionStatsSensor(_Base):
    """Dispatch / dedup / failure counters from the audit trail (F-009)."""

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "decision_stats", "决策统计")
        self._attr_icon = "mdi:clipboard-list"

    @property
    def native_value(self) -> int:
        return int((self._data.get("stats") or {}).get("dispatched", 0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._data.get("stats", {})

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {"stats": {}}
