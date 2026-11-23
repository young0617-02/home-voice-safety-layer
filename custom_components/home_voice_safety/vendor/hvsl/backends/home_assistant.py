"""Home Assistant REST backend (F-002/F-003 adapter).

Talks to a local HA instance over ``/api/states`` and ``/api/services/<domain>/<service>``.
Safety-relevant metadata (display name, ``danger:*`` tags, real/simulated
provenance) is taken from the reviewed local catalog rather than from whatever a
device reports about itself, so a mislabelled device cannot downgrade its own
risk class.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import httpx

from ..catalog import load_domain_words
from ..errors import BackendUnavailableError, DispatchTimeoutError, PreDispatchError
from ..models import DeviceEntity, Provenance
from ..tools import ToolDef
from .base import DeviceBackend, resolve_adjust_args


def _parse_real_entities(raw: str | Iterable[str] | None) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    return {str(item) for item in raw}


def _to_ha_payload(args: dict[str, Any]) -> dict[str, Any]:
    """Translate tool arguments (our JSON Schema) into HA service field names."""
    payload: dict[str, Any] = {}
    for key, value in args.items():
        if key in {"entity_ids"} or value is None:
            continue
        if key == "brightness_pct":
            payload["brightness"] = max(1, min(255, round(int(value) * 2.54)))
        elif key == "volume_level":
            payload["volume_level"] = round(int(value) / 100.0, 3)
        elif key == "temperature_c":
            payload["temperature"] = float(value)
        elif key == "mode":
            payload["hvac_mode"] = str(value)
        else:
            payload[key] = value
    return payload


def load_catalog_overlay(catalog_path: str | Path | None) -> dict[str, dict[str, Any]]:
    """Reviewed device metadata keyed by entity_id (tags, names, aliases)."""
    if not catalog_path or not Path(catalog_path).is_file():
        return {}
    data = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
    return {spec["entity_id"]: spec for spec in data.get("devices", [])}


def build_entity(
    entity_id: str,
    state: str,
    attributes: dict[str, Any],
    overlay: dict[str, Any] | None,
    real_ids: set[str],
) -> DeviceEntity:
    """Merge one HA state with the reviewed catalog.

    Danger tags always come from the catalog, never from the device's own
    attributes: a device must not be able to talk itself out of the danger list.
    ``reviewed`` likewise comes from the catalog's presence, not from any field the
    device reports: entities missing from ``devices/devices.json`` are unreviewed
    and the risk policy keeps their dangerous-domain actions behind a confirmation.
    Shared with ``custom_components/home_voice_safety/backend.py``.
    """
    domain = entity_id.split(".", 1)[0]
    spec = overlay or {}
    reviewed = overlay is not None
    tags = list(spec.get("tags", []))
    if domain in {"lock", "alarm_control_panel"} and "danger:security" not in tags:
        tags.append("danger:security")
    return DeviceEntity(
        entity_id=entity_id,
        domain=domain,
        display_name=spec.get("display_name") or str(attributes.get("friendly_name") or entity_id),
        provenance=Provenance.REAL if entity_id in real_ids else Provenance.SIMULATED,
        state=str(state or "unknown"),
        tags=tags,
        supported_features=list(spec.get("supported_features", [])),
        attributes=dict(attributes or {}),
        aliases=list(spec.get("aliases", [])),
        reviewed=reviewed,
    )


class HomeAssistantBackend(DeviceBackend):
    kind = "home_assistant"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 8.0,
        real_entities: str | Iterable[str] | None = None,
        catalog_path: str | Path | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not base_url:
            raise BackendUnavailableError("HA_BASE_URL is not configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._real = _parse_real_entities(real_entities)
        self._client = client or httpx.Client(timeout=timeout)
        self._overlay = load_catalog_overlay(catalog_path)
        self.domain_words = load_domain_words(catalog_path)
        self.dispatches: list[dict[str, Any]] = []

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}

    def entities(self) -> list[DeviceEntity]:
        try:
            response = self._client.get(f"{self.base_url}/api/states", headers=self._headers(), timeout=self._timeout)
        except httpx.TimeoutException as exc:
            raise DispatchTimeoutError(f"HA /api/states timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(f"HA /api/states unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise BackendUnavailableError(f"HA /api/states returned {response.status_code}")

        out: list[DeviceEntity] = []
        for item in response.json():
            out.append(
                build_entity(
                    item["entity_id"],
                    item.get("state", "unknown"),
                    item.get("attributes") or {},
                    self._overlay.get(item["entity_id"]),
                    self._real,
                )
            )
        return out

    def get(self, entity_id: str) -> DeviceEntity | None:
        for entity in self.entities():
            if entity.entity_id == entity_id:
                return entity
        return None

    def _resolve_adjust_args(self, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        """Resolve a relative delta against the live ``/api/states`` snapshot.

        This happens before the service call: if the current state is missing, the
        request fails with :class:`PreDispatchError` and **nothing** is POSTed.
        """
        if len(entity_ids) != 1:
            raise PreDispatchError("相对调节一次只支持一个设备，未下发")
        entity = self.get(entity_ids[0])
        if entity is None:
            raise PreDispatchError(f"读不到实体 {entity_ids[0]!r} 的当前状态，未下发")
        resolved = resolve_adjust_args(entity, int(args["delta_pct"]))
        return {**resolved, "entity_ids": list(entity_ids)}

    def call_tool(self, tool: ToolDef, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        domain, service = tool.ha_service
        if "delta_pct" in args:
            args = self._resolve_adjust_args(entity_ids, args)
        payload = _to_ha_payload(args)
        payload["entity_id"] = entity_ids if len(entity_ids) > 1 else entity_ids[0]
        url = f"{self.base_url}/api/services/{domain}/{service}"
        try:
            response = self._client.post(url, headers=self._headers(), json=payload, timeout=self._timeout)
        except httpx.TimeoutException as exc:
            # The request may or may not have been applied before the timeout.
            raise DispatchTimeoutError(f"HA service call {tool.name} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BackendUnavailableError(f"HA service call {tool.name} failed: {exc}") from exc
        if response.status_code >= 400:
            raise BackendUnavailableError(
                f"HA service call {tool.name} returned {response.status_code}: {response.text[:200]}"
            )
        self.dispatches.append({"tool": tool.name, "entity_ids": list(entity_ids), "args": payload})
        return {"ok": True, "backend": self.kind, "service": f"{domain}.{service}", "response": response.json()}

    def close(self) -> None:
        self._client.close()
