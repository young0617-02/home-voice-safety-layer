"""Deterministic in-memory backend: mock entities that let the whole chain run offline.

Used as the default everywhere (tests, evals, demo) so a reviewer can clone the
repo and reproduce A1/A2 without Home Assistant or a physical device.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..catalog import load_domain_words as _load_domain_words
from ..catalog import load_entities
from ..errors import BackendUnavailableError, PreDispatchError
from ..models import DeviceEntity
from ..tools import ToolDef
from .base import DeviceBackend, resolve_adjust_args

__all__ = ["SimulatedBackend", "load_catalog", "load_domain_words"]


def load_catalog(path: str | Path) -> list[DeviceEntity]:
    """Kept as the historical name for :func:`hvsl.catalog.load_entities`."""
    return load_entities(path)


def load_domain_words(path: str | Path) -> dict[str, list[str]]:
    """Reviewed generic device vocabulary (「灯」「插座」「锁」...) used by the resolver."""
    return _load_domain_words(path)


class SimulatedBackend(DeviceBackend):
    kind = "simulated"

    def __init__(self, entities: list[DeviceEntity], latency_ms: float = 0.0, domain_words: dict[str, list[str]] | None = None) -> None:
        self._entities = {e.entity_id: e.model_copy(deep=True) for e in entities}
        self.latency_ms = latency_ms
        self.domain_words = domain_words or {}
        self.dispatches: list[dict[str, Any]] = []
        self._failures: dict[str, str] = {}

    @classmethod
    def from_catalog(cls, path: str | Path, latency_ms: float = 0.0) -> "SimulatedBackend":
        return cls(load_catalog(path), latency_ms=latency_ms, domain_words=load_domain_words(path))

    def entities(self) -> list[DeviceEntity]:
        return [self._entities[k] for k in sorted(self._entities)]

    def get(self, entity_id: str) -> DeviceEntity | None:
        return self._entities.get(entity_id)

    def set_failure(self, entity_id: str, message: str | None) -> None:
        """Test hook: make the next dispatch to this entity raise, to exercise the
        pre-write / write-back path of F-008."""
        if message is None:
            self._failures.pop(entity_id, None)
        else:
            self._failures[entity_id] = message

    @property
    def dispatch_count(self) -> int:
        return len(self.dispatches)

    def reset(self) -> None:
        self.dispatches.clear()
        self._failures.clear()

    def call_tool(self, tool: ToolDef, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        for entity_id in entity_ids:
            if entity_id not in self._entities:
                raise PreDispatchError(f"unknown entity {entity_id!r} in simulated backend")
        # Relative adjustments are resolved against the *current* state before any
        # entity is touched.  A failure here proves nothing was dispatched, so the
        # engine may release the idempotency key (PreDispatchError).
        dispatch_args = self._resolve_relative_args(entity_ids, args)
        if self.latency_ms:
            time.sleep(self.latency_ms / 1000.0)
        for entity_id in entity_ids:
            failure = self._failures.get(entity_id)
            if failure:
                # The command reached the device layer, so the outcome is unknown.
                raise BackendUnavailableError(failure)
            self._apply(tool, entity_id, dispatch_args)
        # Record the absolute value that was actually sent, not only the delta.
        self.dispatches.append(
            {"tool": tool.name, "entity_ids": list(entity_ids), "args": dict(dispatch_args), "ts": time.time()}
        )
        return {"ok": True, "backend": self.kind, "entity_ids": entity_ids}

    def _resolve_relative_args(self, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        if "delta_pct" not in args:
            return dict(args)
        if len(entity_ids) != 1:
            raise PreDispatchError("相对调节一次只支持一个设备，未下发")
        entity = self._entities[entity_ids[0]]
        resolved = resolve_adjust_args(entity, int(args["delta_pct"]))
        return {**resolved, "entity_ids": list(entity_ids)}

    def _apply(self, tool: ToolDef, entity_id: str, args: dict[str, Any]) -> None:
        entity = self._entities[entity_id]
        domain, service = tool.ha_service
        attributes = entity.attributes

        if domain == "light":
            if service == "turn_on":
                entity.state = "on"
                if args.get("brightness_pct") is not None:
                    attributes["brightness"] = round(int(args["brightness_pct"]) * 2.54)
            else:
                entity.state = "off"
        elif domain == "switch":
            entity.state = "on" if service == "turn_on" else "off"
        elif domain == "lock":
            entity.state = "locked" if service == "lock" else "unlocked"
        elif domain == "climate":
            if service == "set_temperature":
                attributes["temperature"] = float(args["temperature_c"])
                if entity.state == "off":
                    entity.state = "heat"
            elif service == "set_hvac_mode":
                attributes["hvac_mode"] = str(args["mode"])
            else:
                entity.state = "off" if service == "turn_off" else attributes.get("hvac_mode", "heat")
        elif domain == "cover":
            if service == "open_cover":
                entity.state = "open"
                attributes["current_position"] = 100
            elif service == "close_cover":
                entity.state = "closed"
                attributes["current_position"] = 0
            else:
                attributes["current_position"] = int(args["percentage"])
                entity.state = "closed" if attributes["current_position"] == 0 else "open"
        elif domain == "alarm_control_panel":
            entity.state = {
                "alarm_arm_away": "armed_away",
                "alarm_arm_home": "armed_home",
                "alarm_disarm": "disarmed",
            }[service]
        elif domain == "fan":
            if service == "set_percentage":
                attributes["percentage"] = int(args["percentage"])
                entity.state = "on" if args["percentage"] else "off"
            else:
                entity.state = "on" if service == "turn_on" else "off"
        elif domain == "vacuum":
            entity.state = "cleaning" if service == "start" else "returning"
        elif domain == "media_player":
            if service == "volume_set":
                attributes["volume_level"] = round(int(args["volume_level"]) / 100.0, 3)
            else:
                entity.state = "off"
        else:  # pragma: no cover - registry only ships known domains
            raise PreDispatchError(f"simulated backend has no handler for {domain}.{service}")
