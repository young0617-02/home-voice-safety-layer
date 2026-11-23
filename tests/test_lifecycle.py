"""P1-3 lifecycle: unload safety, 1->2 migration, remove_data and diagnostics privacy.

Home Assistant is not installed in this repo; these tests drive the real
``__init__.py``/``diagnostics.py`` through duck-typed fakes for the small HA
surface they use.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import custom_components.home_voice_safety as integration
from custom_components.home_voice_safety import diagnostics
from custom_components.home_voice_safety.const import (
    CONFIG_ENTRY_VERSION,
    CONF_CONFIRM_TTL,
    CONF_DEVICE_CATALOG,
    CONF_NOTIFY_SERVICE,
    CONF_POLICY_PATH,
    CONF_REMOVE_DATA,
    DOMAIN,
)
from custom_components.home_voice_safety.services import SERVICES


class FakeConfigEntries:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def async_unload_platforms(self, entry, platforms) -> bool:
        return True

    def async_update_entry(self, entry, **kwargs) -> None:
        self.updates.append(kwargs)
        for key, value in kwargs.items():
            setattr(entry, key, value)


class FakeServices:
    def __init__(self) -> None:
        self.removed: list[tuple[str, str]] = []

    def async_remove(self, domain: str, service: str) -> None:
        self.removed.append((domain, service))


class FakeHass:
    def __init__(self, config_dir: Path) -> None:
        self.data: dict = {}
        self.config = SimpleNamespace(config_dir=str(config_dir))
        self.config_entries = FakeConfigEntries()
        self.services = FakeServices()

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeEngine:
    def __init__(self, db_path: Path) -> None:
        self.settings = SimpleNamespace(db_path=str(db_path))
        self.closed = False
        self.gate: list = []

    def close(self) -> None:
        self.closed = True


class FakeCoordinator:
    def __init__(self, db_path: Path) -> None:
        self.engine = FakeEngine(db_path)


def _entry(*, version: int = CONFIG_ENTRY_VERSION, data=None, options=None):
    return SimpleNamespace(
        entry_id="entry-1",
        version=version,
        title="Home Voice Safety Layer",
        data=dict(data or {}),
        options=dict(options or {}),
    )


def test_unload_last_entry_removes_services_and_keeps_db(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)
    db_path = tmp_path / "home_voice_safety_decisions.db"
    db_path.write_text("audit", encoding="utf-8")
    coordinator = FakeCoordinator(db_path)
    entry = _entry()
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True

    assert coordinator.engine.closed is True
    assert db_path.exists(), "remove_data 默认必须保留审计 DB"
    assert {(DOMAIN, service) for service in SERVICES} <= set(hass.services.removed)
    assert not hass.data.get(DOMAIN)


def test_unload_keeps_services_while_another_entry_remains(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)
    entry = _entry()
    hass.data[DOMAIN] = {entry.entry_id: FakeCoordinator(tmp_path / "a.db"), "other": object()}

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True

    assert hass.services.removed == []
    assert "other" in hass.data[DOMAIN]


def test_unload_after_failed_setup_does_not_key_error(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)
    entry = _entry()

    # Simulates async_setup_entry having failed before hass.data[DOMAIN] existed.
    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert {(DOMAIN, service) for service in SERVICES} <= set(hass.services.removed)


def test_remove_data_true_deletes_db_and_sidecars(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)
    db_path = tmp_path / "home_voice_safety_decisions.db"
    for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        path.write_text("x", encoding="utf-8")
    entry = _entry(options={CONF_REMOVE_DATA: True})
    hass.data[DOMAIN] = {entry.entry_id: FakeCoordinator(db_path)}

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True

    assert not db_path.exists()
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


def test_migrate_v1_to_v2_is_additive_and_keeps_user_options(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)
    entry = _entry(
        version=1,
        data={CONF_DEVICE_CATALOG: "catalog.json", CONF_POLICY_PATH: "policy.json"},
        options={CONF_CONFIRM_TTL: 77},
    )

    assert asyncio.run(integration.async_migrate_entry(hass, entry)) is True

    assert entry.version == CONFIG_ENTRY_VERSION == 2
    assert entry.data[CONF_DEVICE_CATALOG] == "catalog.json"
    assert entry.data[CONF_POLICY_PATH] == "policy.json"
    assert entry.options[CONF_CONFIRM_TTL] == 77
    assert entry.options[CONF_REMOVE_DATA] is False
    assert entry.options[CONF_NOTIFY_SERVICE] == ""
    assert len(hass.config_entries.updates) == 1


def test_migrate_unsupported_or_missing_resources_rolls_back(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)

    future = _entry(version=3)
    assert asyncio.run(integration.async_migrate_entry(hass, future)) is False
    assert future.version == 3
    assert hass.config_entries.updates == []

    # v1 with no paths and no bootstrap files: fail closed, keep v1 untouched.
    broken = _entry(version=1, data={}, options={})
    assert asyncio.run(integration.async_migrate_entry(hass, broken)) is False
    assert broken.version == 1
    assert hass.config_entries.updates == []


def test_diagnostics_never_leaks_token_or_utterance(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path)
    coordinator = SimpleNamespace(
        data={
            "entities": 3,
            "real_devices": 1,
            "simulated_devices": 2,
            "dangerous_entities": 1,
            "pending": [
                {
                    "token": "TOKEN-MUST-NOT-LEAK",
                    "utterance": "SECRET-UTTERANCE",
                    "risk_class": "door_unlock",
                    "expires_in_seconds": 42,
                }
            ],
            "stats": {"dispatched": 2, "suppressed": 1},
        },
        engine=SimpleNamespace(stats=lambda: {"dispatched": 2, "pending": 1}),
    )
    entry = _entry(
        data={
            CONF_DEVICE_CATALOG: str(tmp_path / "devices.json"),
            CONF_POLICY_PATH: str(tmp_path / "risk_policy.json"),
        },
        options={CONF_REMOVE_DATA: False},
    )
    hass.data[DOMAIN] = {entry.entry_id: coordinator}

    payload = asyncio.run(diagnostics.async_get_config_entry_diagnostics(hass, entry))
    blob = json.dumps(payload, ensure_ascii=False)

    assert "TOKEN-MUST-NOT-LEAK" not in blob
    assert "SECRET-UTTERANCE" not in blob
    assert "utterance" not in blob
    assert "token" not in blob
    assert payload["runtime"]["pending"]["count"] == 1
    assert payload["runtime"]["pending"]["risk_classes"] == ["door_unlock"]
    assert payload["runtime"]["engine_stats"] == {"dispatched": 2, "pending": 1}
    assert payload["runtime"]["sensor_summary"]["pending_actions"] == 1