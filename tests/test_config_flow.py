"""P0-3: config-flow safety validation, first-run wizard and options reload.

Home Assistant is not a dev dependency in this repo (the real test harness package
pins a HA build that is unavailable here), so this module installs a minimal set
of HA/voluptuous stubs and exercises the flow methods directly.  It does **not**
skip: every assertion below runs against the real ``config_flow.py``.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _install_voluptuous_stub() -> None:
    try:
        import voluptuous  # noqa: F401
        return
    except ModuleNotFoundError:
        pass

    vol = types.ModuleType("voluptuous")

    class _Marker:
        def __init__(self, key: str, default: Any = None) -> None:
            self.schema = key
            self.default = default

        def __hash__(self) -> int:
            return id(self)

    class _Schema:
        def __init__(self, schema: dict[Any, Any]) -> None:
            self.schema = schema

        def __getitem__(self, key: Any) -> Any:
            return self.schema[key]

    def _required(key: str, default: Any = None, **_kwargs: Any) -> _Marker:
        return _Marker(key, default)

    def _optional(key: str, default: Any = None, **_kwargs: Any) -> _Marker:
        return _Marker(key, default)

    def _schema(schema: dict[Any, Any]) -> _Schema:
        return _Schema(dict(schema))

    def _all(*validators: Any) -> Any:
        return validators[-1] if validators else None

    def _any(*validators: Any) -> tuple[Any, ...]:
        return validators

    def _coerce(value_type: Any) -> Any:
        return value_type

    def _range(**_kwargs: Any) -> None:
        return None

    def _in(container: Any) -> Any:
        return container

    vol.Schema = _schema
    vol.Required = _required
    vol.Optional = _optional
    vol.All = _all
    vol.Any = _any
    vol.Coerce = _coerce
    vol.Range = _range
    vol.In = _in
    sys.modules["voluptuous"] = vol


def _install_stubs() -> None:
    _install_voluptuous_stub()
    _install_ha_stubs()


def _install_ha_stubs() -> None:
    """Install just enough HA surface for importing and driving the flow methods."""
    ha = types.ModuleType("homeassistant")

    class ConfigEntry:
        def __init__(
            self,
            entry_id: str = "entry-1",
            data: dict[str, Any] | None = None,
            options: dict[str, Any] | None = None,
        ) -> None:
            self.entry_id = entry_id
            self.data = dict(data or {})
            self.options = dict(options or {})

    class ConfigFlow:
        def __init__(self) -> None:
            self.hass: Any = None
            self.context: dict[str, Any] = {}
            self._unique_id: str | None = None

        def __init_subclass__(cls, *, domain: str | None = None, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            cls.domain = domain

        def async_show_form(self, **kwargs: Any) -> dict[str, Any]:
            return {"type": "form", **kwargs}

        def async_create_entry(self, *, title: str, data: dict[str, Any]) -> dict[str, Any]:
            return {"type": "create_entry", "title": title, "data": data}

        def async_abort(self, *, reason: str) -> dict[str, Any]:
            return {"type": "abort", "reason": reason}

        async def async_set_unique_id(self, unique_id: str) -> None:
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            return None

    class OptionsFlowWithConfigEntry(ConfigFlow):
        def __init__(self, config_entry: ConfigEntry) -> None:
            super().__init__()
            self.config_entry = config_entry
            self.hass = getattr(config_entry, "hass", None)

    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = ConfigFlow
    config_entries.ConfigFlowResult = dict
    config_entries.OptionsFlowWithConfigEntry = OptionsFlowWithConfigEntry

    core = types.ModuleType("homeassistant.core")

    def callback(func: Any) -> Any:
        return func

    core.callback = callback
    core.HomeAssistant = object

    helpers = types.ModuleType("homeassistant.helpers")
    selector = types.ModuleType("homeassistant.helpers.selector")

    class SelectSelectorConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.options = kwargs.get("options", [])
            self.multiple = bool(kwargs.get("multiple", False))

    class SelectSelector:
        def __init__(self, config: SelectSelectorConfig) -> None:
            self.config = config

        def __call__(self, value: Any) -> Any:
            # Real HA selectors are callable voluptuous validators; the stub only
            # needs to stay compilable when the real ``voluptuous`` is installed.
            return value

    selector.SelectSelectorConfig = SelectSelectorConfig
    selector.SelectSelector = SelectSelector
    helpers.selector = selector

    ha.config_entries = config_entries
    ha.core = core
    ha.helpers = helpers
    sys.modules.update(
        {
            "homeassistant": ha,
            "homeassistant.config_entries": config_entries,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.selector": selector,
        }
    )


try:
    import homeassistant  # noqa: F401
except ModuleNotFoundError:
    _install_stubs()

config_flow = importlib.import_module("custom_components.home_voice_safety.config_flow")
const = importlib.import_module("custom_components.home_voice_safety.const")

CONF_DEVICE_CATALOG = config_flow.CONF_DEVICE_CATALOG
CONF_POLICY_PATH = config_flow.CONF_POLICY_PATH
CONF_DANGEROUS_ENTITIES = const.CONF_DANGEROUS_ENTITIES

POLICY_PATH = str(config_flow._BUNDLED_POLICY)


class FakeState:
    def __init__(self, entity_id: str, friendly_name: str) -> None:
        self.entity_id = entity_id
        self.attributes = {"friendly_name": friendly_name}


class FakeStates:
    def __init__(self, states: list[FakeState]) -> None:
        self._states = states

    def async_all(self) -> list[FakeState]:
        return list(self._states)


class FakeConfig:
    def __init__(self, config_dir: str) -> None:
        self.config_dir = config_dir


class FakeConfigEntries:
    def __init__(self) -> None:
        self.reload_calls: list[str] = []

    async def async_reload(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)


class FakeHass:
    def __init__(self, config_dir: Path, states: list[FakeState]) -> None:
        self.config = FakeConfig(str(config_dir))
        self.states = FakeStates(states)
        self.config_entries = FakeConfigEntries()
        self.data: dict[str, Any] = {}

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        return func(*args)


def _states() -> list[FakeState]:
    return [
        FakeState("lock.front_door", "入户门锁"),
        FakeState("switch.gas_valve", "燃气阀"),
        FakeState("light.living_room", "客厅灯"),
        FakeState("sensor.temperature", "温度"),
    ]


def _valid_catalog(tmp_path: Path) -> str:
    path = tmp_path / "devices.json"
    path.write_text(
        json.dumps(
            {
                "devices": [
                    {
                        "entity_id": "lock.front_door",
                        "display_name": "入户门锁",
                        "tags": ["danger:security"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return str(path)


def _new_flow(hass: FakeHass):
    flow = config_flow.HomeVoiceSafetyConfigFlow()
    flow.hass = hass
    return flow


def _schema_field(schema: Any, key: str) -> Any:
    """Return the validator for ``key`` from a real or stubbed voluptuous schema."""
    raw = getattr(schema, "schema", schema)
    for marker, validator in raw.items():
        if getattr(marker, "schema", marker) == key:
            return validator
    raise KeyError(key)


def test_candidate_entities_are_listed_and_unselected_cannot_finish(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path, _states())
    flow = _new_flow(hass)

    form = asyncio.run(flow.async_step_user())
    assert form["type"] == "form"
    assert form["step_id"] == "user"

    selector = _schema_field(form["data_schema"], CONF_DANGEROUS_ENTITIES)
    values = {option["value"] for option in selector.config.options}
    assert values == {"lock.front_door", "switch.gas_valve"}
    assert "light.living_room" not in values
    assert selector.config.multiple is True

    rejected = asyncio.run(flow.async_step_user({CONF_DANGEROUS_ENTITIES: []}))
    assert rejected["type"] == "form"
    assert rejected["errors"][CONF_DANGEROUS_ENTITIES] == "no_dangerous_entities_selected"
    assert not (tmp_path / "home_voice_safety" / "devices.json").exists()


def test_wizard_writes_reviewed_catalog_and_confirm_policy(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path, _states())
    flow = _new_flow(hass)

    result = asyncio.run(
        flow.async_step_user(
            {CONF_DANGEROUS_ENTITIES: ["lock.front_door", "switch.gas_valve"]}
        )
    )
    assert result["type"] == "form"
    assert result["step_id"] == "tuning"

    catalog_path = tmp_path / "home_voice_safety" / "devices.json"
    policy_path = tmp_path / "home_voice_safety" / "risk_policy.json"
    assert catalog_path.is_file()
    assert policy_path.is_file()

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    entities = {item["entity_id"]: item for item in catalog["devices"]}
    assert {"lock.front_door", "switch.gas_valve", "light.living_room"} <= set(entities)
    # 选中设备必须带 danger 标签；未选中的可控设备（灯）不带标签，保持低风险。
    assert entities["lock.front_door"]["tags"] == ["danger:user_selected"]
    assert entities["switch.gas_valve"]["tags"] == ["danger:user_selected"]
    assert entities["light.living_room"]["tags"] == []
    assert "sensor.temperature" not in entities
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    assert any(rule["id"] == "user_selected_device" for rule in policy["rules"])

    entry = asyncio.run(flow.async_step_tuning({const.CONF_CONFIRM_TTL: 60}))
    assert entry["type"] == "create_entry"
    assert entry["data"][CONF_DEVICE_CATALOG] == str(catalog_path)
    assert entry["data"][CONF_POLICY_PATH] == str(policy_path)
    assert entry["data"][CONF_DANGEROUS_ENTITIES] == ["lock.front_door", "switch.gas_valve"]


def test_import_rejects_empty_and_broken_safety_inputs(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path, _states())
    flow = _new_flow(hass)

    empty = asyncio.run(flow.async_step_import({}))
    assert empty["type"] == "abort"
    assert empty["reason"] == "catalog_missing"

    bad_catalog_path = tmp_path / "bad_catalog.json"
    bad_catalog_path.write_text("{}", encoding="utf-8")
    bad_catalog = asyncio.run(
        flow.async_step_import(
            {
                CONF_DEVICE_CATALOG: str(bad_catalog_path),
                CONF_POLICY_PATH: POLICY_PATH,
            }
        )
    )
    assert bad_catalog["type"] == "abort"
    assert bad_catalog["reason"] == "catalog_invalid"

    bad_policy_path = tmp_path / "bad_policy.json"
    bad_policy_path.write_text("{}", encoding="utf-8")
    bad_policy = asyncio.run(
        flow.async_step_import(
            {
                CONF_DEVICE_CATALOG: _valid_catalog(tmp_path),
                CONF_POLICY_PATH: str(bad_policy_path),
            }
        )
    )
    assert bad_policy["type"] == "abort"
    assert bad_policy["reason"] == "policy_invalid"


def test_import_with_valid_paths_creates_entry(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path, _states())
    flow = _new_flow(hass)

    result = asyncio.run(
        flow.async_step_import(
            {
                CONF_DEVICE_CATALOG: _valid_catalog(tmp_path),
                CONF_POLICY_PATH: POLICY_PATH,
                const.CONF_CONFIRM_TTL: 45,
            }
        )
    )
    assert result["type"] == "create_entry"
    assert result["data"][const.CONF_CONFIRM_TTL] == 45


def test_options_validate_and_reload_once(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path, _states())
    entry = types.SimpleNamespace(
        entry_id="entry-1",
        data={
            CONF_DEVICE_CATALOG: _valid_catalog(tmp_path),
            CONF_POLICY_PATH: POLICY_PATH,
        },
        options={},
    )
    entry.hass = hass

    flow = config_flow.HomeVoiceSafetyOptionsFlow(entry)
    flow.hass = hass
    saved = asyncio.run(flow.async_step_init({const.CONF_CONFIRM_TTL: 90}))
    assert saved["type"] == "create_entry"
    assert saved["data"][const.CONF_CONFIRM_TTL] == 90

    bad = asyncio.run(
        flow.async_step_init({CONF_DEVICE_CATALOG: str(tmp_path / "missing.json")})
    )
    assert bad["type"] == "form"
    assert bad["errors"][CONF_DEVICE_CATALOG] == "catalog_missing"

    hass.data[const.DOMAIN] = {
        "entry-1": types.SimpleNamespace(engine=types.SimpleNamespace(gate=[]))
    }
    asyncio.run(config_flow.async_update_options(hass, entry))
    assert hass.config_entries.reload_calls == ["entry-1"]


def test_options_reload_is_deferred_while_confirmation_is_pending(tmp_path: Path) -> None:
    hass = FakeHass(tmp_path, _states())
    entry = types.SimpleNamespace(entry_id="entry-1", data={}, options={})
    entry.hass = hass
    hass.data[const.DOMAIN] = {
        "entry-1": types.SimpleNamespace(engine=types.SimpleNamespace(gate=[object()]))
    }

    asyncio.run(config_flow.async_update_options(hass, entry))
    assert hass.config_entries.reload_calls == []


def test_session_prefix_dead_config_is_removed() -> None:
    assert not hasattr(const, "CONF_SESSION_PREFIX")
    assert "session_prefix" not in Path(
        "custom_components/home_voice_safety/strings.json"
    ).read_text(encoding="utf-8")

def test_config_entry_version_is_two() -> None:
    assert config_flow.HomeVoiceSafetyConfigFlow.VERSION == 2
