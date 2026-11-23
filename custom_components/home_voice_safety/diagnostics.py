"""Diagnostics for the Home Voice Safety Layer integration (P1-5).

HA diagnostics are meant for support tickets, so this file is deliberately a
**whitelist**: paths, tuning values, counters and aggregate sensor state.  It
never dumps raw pending actions, confirmation tokens, utterances or conversation
history - those live in the SQLite audit trail and stay there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .const import (
    CONF_CONFIRM_TTL,
    CONF_DANGEROUS_ENTITIES,
    CONF_DEDUP_WINDOW,
    CONF_DEVICE_CATALOG,
    CONF_NLU_PROVIDER,
    CONF_NOTIFY_SERVICE,
    CONF_POLICY_PATH,
    CONF_REAL_ENTITIES,
    CONF_REMOVE_DATA,
    DOMAIN,
)


def _safe_path(value: Any) -> str:
    """Keep the path useful but strip the local home directory when possible."""
    text = str(value or "")
    home = str(Path.home())
    if text and home and text.startswith(home):
        return "~" + text[len(home) :]
    return text


def _pending_summary(coordinator) -> dict[str, Any]:
    """Aggregate parked actions without leaking token/session/utterance."""
    data = getattr(coordinator, "data", None) or {}
    pending = list(data.get("pending", []) or [])
    return {
        "count": len(pending),
        "risk_classes": sorted(
            {str(item.get("risk_class", "")) for item in pending if item.get("risk_class")}
        ),
        "expires_in_seconds": sorted(
            {float(item.get("expires_in_seconds", 0) or 0) for item in pending}
        ),
    }


def _engine_stats(coordinator) -> dict[str, int]:
    engine = getattr(coordinator, "engine", None)
    stats = getattr(engine, "stats", None)
    if not callable(stats):
        return {}
    try:
        return {str(key): int(value) for key, value in (stats() or {}).items()}
    except Exception:  # noqa: BLE001 - diagnostics must never take HA down
        return {}


async def async_get_config_entry_diagnostics(hass, entry) -> dict[str, Any]:
    """Return support-safe diagnostics for one config entry."""
    merged = {**getattr(entry, "data", {}), **getattr(entry, "options", {})}
    diagnostics: dict[str, Any] = {
        "entry": {
            "version": getattr(entry, "version", None),
            "title": getattr(entry, "title", ""),
            "domain": DOMAIN,
        },
        "config": {
            "device_catalog": _safe_path(merged.get(CONF_DEVICE_CATALOG)),
            "policy_path": _safe_path(merged.get(CONF_POLICY_PATH)),
            "confirm_ttl_seconds": merged.get(CONF_CONFIRM_TTL),
            "dedup_window_seconds": merged.get(CONF_DEDUP_WINDOW),
            "nlu_provider": merged.get(CONF_NLU_PROVIDER),
            "real_entities": merged.get(CONF_REAL_ENTITIES, ""),
            "notify_service": merged.get(CONF_NOTIFY_SERVICE, ""),
            "remove_data": bool(merged.get(CONF_REMOVE_DATA, False)),
            "dangerous_entity_count": len(merged.get(CONF_DANGEROUS_ENTITIES, []) or []),
        },
        "runtime": {"loaded": False},
    }

    coordinator = (hass.data.get(DOMAIN) or {}).get(getattr(entry, "entry_id", ""))
    if coordinator is None:
        return diagnostics

    data = getattr(coordinator, "data", None) or {}
    diagnostics["runtime"] = {
        "loaded": True,
        "entities": int(data.get("entities", 0) or 0),
        "real_devices": int(data.get("real_devices", 0) or 0),
        "simulated_devices": int(data.get("simulated_devices", 0) or 0),
        "dangerous_entities": int(data.get("dangerous_entities", 0) or 0),
        "pending": _pending_summary(coordinator),
        "engine_stats": _engine_stats(coordinator),
        "sensor_summary": {
            "pending_actions": int(data.get("pending_count", 0) or len(data.get("pending", []) or [])),
            "device_inventory": {
                "real": int(data.get("real_devices", 0) or 0),
                "simulated": int(data.get("simulated_devices", 0) or 0),
                "dangerous": int(data.get("dangerous_entities", 0) or 0),
            },
            "decision_stats": dict(data.get("stats", {}) or {}),
        },
    }
    return diagnostics