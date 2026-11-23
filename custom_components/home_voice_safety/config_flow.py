"""Config flow for the Home Voice Safety Layer integration (F-002).

Two safety gates are enforced here:

* the reviewed device catalog must exist and parse, and the risk policy must
  load — a typo in either would silently downgrade an action to "low risk", so
  both the user flow and the YAML import flow share :func:`_validate_input`;
* the first-run wizard asks the user to explicitly tick dangerous devices.  It
  writes a catalog plus a policy overlay into ``<config>/home_voice_safety/`` so
  no path typing is required and the selected devices stay in the confirm gate.

Options changes are applied with a config-entry reload.  If there are parked
confirmations, the reload is deferred instead of silently dropping them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONFIG_ENTRY_VERSION,
    CONF_CONFIRM_TTL,
    CONF_DANGEROUS_ENTITIES,
    CONF_DEDUP_WINDOW,
    CONF_DEVICE_CATALOG,
    CONF_NLU_PROVIDER,
    CONF_NOTIFY_SERVICE,
    CONF_POLICY_PATH,
    CONF_REAL_ENTITIES,
    CONF_REMOVE_DATA,
    DEFAULT_CONFIRM_TTL,
    DEFAULT_DEDUP_WINDOW,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

#: First-run candidates.  These are the domains whose actions can hurt a home;
#: lights/fans/media players are intentionally absent from the wizard.
_CANDIDATE_DOMAINS = frozenset(
    {"lock", "switch", "cover", "alarm_control_panel", "vacuum", "climate"}
)
#: Domains the catalog should expose so non-dangerous devices (lights, fans,
#: media players) remain controllable without a confirmation prompt.
_CONTROLLABLE_DOMAINS = frozenset(
    {
        "light",
        "switch",
        "lock",
        "cover",
        "climate",
        "fan",
        "media_player",
        "vacuum",
        "alarm_control_panel",
    }
)
_USER_SELECTED_TAG = "danger:user_selected"
_USER_SELECTED_RULE_ID = "user_selected_device"
_BOOTSTRAP_DIR = "home_voice_safety"

#: The reviewed policy ships with the integration (P0-1 vendor copy).  Do not
#: import it through coordinator: that module is owned by another workstream.
_BUNDLED_POLICY = Path(__file__).resolve().parent / "vendor" / "hvsl" / "policy" / "risk_policy.json"


TUNING_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CONFIRM_TTL, default=DEFAULT_CONFIRM_TTL): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=3600)
        ),
        vol.Optional(CONF_DEDUP_WINDOW, default=DEFAULT_DEDUP_WINDOW): vol.All(
            vol.Coerce(float), vol.Range(min=1, max=3600)
        ),
        vol.Optional(CONF_NLU_PROVIDER, default="rule"): vol.In(["rule", "llm"]),
        vol.Optional(CONF_REAL_ENTITIES, default=""): str,
        # 可选通知服务（如 notify.hvs_file）；留空则只 fire 事件，不推手机通知。
        vol.Optional(CONF_NOTIFY_SERVICE, default=""): str,
    }
)


def _as_list(raw: Any) -> list[str]:
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if raw is None:
        return []
    return [str(item) for item in raw]


def _clean_path(raw: Any) -> str:
    return str(raw or "").strip()


def _candidate_specs(states: list[Any]) -> list[dict[str, Any]]:
    """Return the dangerous-domain entities the first-run wizard can offer."""
    specs: list[dict[str, Any]] = []
    for state in states:
        entity_id = str(getattr(state, "entity_id", "") or "")
        domain = entity_id.split(".", 1)[0]
        if domain not in _CANDIDATE_DOMAINS:
            continue
        attributes = dict(getattr(state, "attributes", {}) or {})
        display_name = str(attributes.get("friendly_name") or entity_id)
        specs.append(
            {
                "entity_id": entity_id,
                "display_name": display_name,
                "tags": [_USER_SELECTED_TAG],
            }
        )
    return sorted(specs, key=lambda spec: spec["entity_id"])


def _catalog_specs(states: list[Any], selected_ids: set[str]) -> list[dict[str, Any]]:
    """All controllable entities; only user-selected ones carry the danger tag.

    The wizard only *offers* dangerous domains, but the catalog must also expose
    lights/fans/media players so low-risk commands still execute.  Selected
    entities get ``danger:user_selected``; everything else stays low risk.
    """
    specs: list[dict[str, Any]] = []
    for state in states:
        entity_id = str(getattr(state, "entity_id", "") or "")
        domain = entity_id.split(".", 1)[0]
        if domain not in _CONTROLLABLE_DOMAINS:
            continue
        attributes = dict(getattr(state, "attributes", {}) or {})
        display_name = str(attributes.get("friendly_name") or entity_id)
        specs.append(
            {
                "entity_id": entity_id,
                "display_name": display_name,
                "tags": [_USER_SELECTED_TAG] if entity_id in selected_ids else [],
            }
        )
    return sorted(specs, key=lambda spec: spec["entity_id"])


def _user_schema(candidates: list[dict[str, Any]]) -> vol.Schema:
    options = [
        {
            "value": candidate["entity_id"],
            "label": f"{candidate['display_name']} ({candidate['entity_id']})",
        }
        for candidate in candidates
    ]
    return vol.Schema(
        {
            vol.Required(CONF_DANGEROUS_ENTITIES, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True)
            ),
        }
    )


def _options_schema(current: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_DEVICE_CATALOG, default=current.get(CONF_DEVICE_CATALOG, "")
            ): str,
            vol.Optional(
                CONF_POLICY_PATH,
                default=current.get(CONF_POLICY_PATH, str(_BUNDLED_POLICY)),
            ): str,
            vol.Optional(
                CONF_CONFIRM_TTL, default=current.get(CONF_CONFIRM_TTL, DEFAULT_CONFIRM_TTL)
            ): vol.All(vol.Coerce(float), vol.Range(min=5, max=3600)),
            vol.Optional(
                CONF_DEDUP_WINDOW, default=current.get(CONF_DEDUP_WINDOW, DEFAULT_DEDUP_WINDOW)
            ): vol.All(vol.Coerce(float), vol.Range(min=1, max=3600)),
            vol.Optional(
                CONF_NLU_PROVIDER, default=current.get(CONF_NLU_PROVIDER, "rule")
            ): vol.In(["rule", "llm"]),
            vol.Optional(
                CONF_REAL_ENTITIES, default=current.get(CONF_REAL_ENTITIES, "")
            ): str,
            # P0-6：可选通知服务；留空则不推手机通知，确认入口仍由事件/服务提供。
            vol.Optional(
                CONF_NOTIFY_SERVICE, default=current.get(CONF_NOTIFY_SERVICE, "")
            ): str,
            # P1-3：默认保留审计 DB；只有显式 true 才在卸载时删除。
            vol.Optional(
                CONF_REMOVE_DATA, default=bool(current.get(CONF_REMOVE_DATA, False))
            ): bool,
        }
    )


def _write_bootstrap_files(
    config_dir: str, selected_specs: list[dict[str, Any]]
) -> tuple[str, str]:
    """Write catalog + policy overlay for the devices chosen in the wizard.

    The overlay adds a single declarative rule (tag ``danger:user_selected`` ->
    confirm for every namespaced tool), so selecting a cover/climate/vacuum has
    the same safety effect as selecting a lock or switch.
    """
    if not _BUNDLED_POLICY.is_file():
        raise RuntimeError(
            f"内置风险策略缺失：{_BUNDLED_POLICY}。"
            "请运行 `python scripts/sync_vendor.py` 后重试。"
        )

    base = Path(config_dir) / _BOOTSTRAP_DIR
    base.mkdir(parents=True, exist_ok=True)
    catalog_path = base / "devices.json"
    policy_path = base / "risk_policy.json"

    catalog_path.write_text(
        json.dumps({"devices": selected_specs}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    from .safety import RiskPolicy

    payload = RiskPolicy.load(_BUNDLED_POLICY).to_dict()
    if not any(rule.get("id") == _USER_SELECTED_RULE_ID for rule in payload["rules"]):
        payload["rules"].append(
            {
                "id": _USER_SELECTED_RULE_ID,
                "risk": "confirm",
                "match": {
                    "tool_prefix_any": ["hvs."],
                    "tag_any": [_USER_SELECTED_TAG],
                },
                "reason": "该设备由用户在首次向导中标记为危险设备，动作必须二次确认。",
                "prompt": (
                    "该指令会操作【{{targets}}】，这是你标记的危险设备。"
                    "请回复确认（confirm）后才会下发。"
                ),
            }
        )
    policy_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return str(catalog_path), str(policy_path)


async def _validate_input(hass, user_input: dict[str, Any]) -> dict[str, str]:
    """Shared safety validation for the user flow and the import flow."""
    errors: dict[str, str] = {}
    catalog = _clean_path(user_input.get(CONF_DEVICE_CATALOG))
    policy = _clean_path(user_input.get(CONF_POLICY_PATH))

    if not catalog or not Path(catalog).is_file():
        errors[CONF_DEVICE_CATALOG] = "catalog_missing"
    elif not await hass.async_add_executor_job(_catalog_is_valid, catalog):
        errors[CONF_DEVICE_CATALOG] = "catalog_invalid"

    if (
        not policy
        or not Path(policy).is_file()
        or not await hass.async_add_executor_job(_policy_is_valid, policy)
    ):
        errors[CONF_POLICY_PATH] = "policy_invalid"

    return errors


def _catalog_is_valid(path: str) -> bool:
    from hvsl.backends.home_assistant import load_catalog_overlay

    overlay = load_catalog_overlay(path)
    return bool(overlay) and all(spec.get("entity_id") for spec in overlay.values())


def _policy_is_valid(path: str) -> bool:
    try:
        from .safety import RiskPolicy

        policy = RiskPolicy.load(path)
    except Exception:  # noqa: BLE001 - any loader error must fail closed
        return False
    return bool(policy.danger_classes())


class HomeVoiceSafetyConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Set up one decision-layer instance per Home Assistant."""

    VERSION = CONFIG_ENTRY_VERSION

    def __init__(self) -> None:
        self._base: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HomeVoiceSafetyOptionsFlow:
        return HomeVoiceSafetyOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        candidates = _candidate_specs(self.hass.states.async_all())
        if not candidates:
            return self.async_abort(reason="no_dangerous_entities")

        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(candidates),
                description_placeholders={"count": str(len(candidates))},
            )

        selected_ids = _as_list(user_input.get(CONF_DANGEROUS_ENTITIES))
        by_id = {candidate["entity_id"]: candidate for candidate in candidates}
        errors: dict[str, str] = {}
        unknown = [entity_id for entity_id in selected_ids if entity_id not in by_id]
        if unknown:
            errors[CONF_DANGEROUS_ENTITIES] = "entities_not_candidates"
        elif not selected_ids:
            errors[CONF_DANGEROUS_ENTITIES] = "no_dangerous_entities_selected"
        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(candidates),
                errors=errors,
                description_placeholders={"count": str(len(candidates))},
            )

        selected_specs = _catalog_specs(self.hass.states.async_all(), set(selected_ids))
        catalog, policy = await self.hass.async_add_executor_job(
            _write_bootstrap_files, self.hass.config.config_dir, selected_specs
        )
        base = {
            **user_input,
            CONF_DEVICE_CATALOG: catalog,
            CONF_POLICY_PATH: policy,
            CONF_DANGEROUS_ENTITIES: selected_ids,
        }
        errors = await _validate_input(self.hass, base)
        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=_user_schema(candidates),
                errors=errors,
                description_placeholders={"count": str(len(candidates))},
            )

        self._base = base
        await self.async_set_unique_id(f"{DOMAIN}-{self.hass.config.config_dir}")
        self._abort_if_unique_id_configured()
        return await self.async_step_tuning()

    async def async_step_tuning(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="tuning", data_schema=TUNING_SCHEMA)
        return self.async_create_entry(
            title="Home Voice Safety Layer", data={**self._base, **user_input}
        )

    async def async_step_import(self, import_data: dict[str, Any]) -> ConfigFlowResult:
        """Scripted bootstrap: same validation as the user flow, never a bypass."""
        errors = await _validate_input(self.hass, import_data)
        if errors:
            # An import flow has no form to re-display, so surface the exact
            # validation key as the abort reason (catalog_missing/policy_invalid).
            reason = (
                errors.get(CONF_DEVICE_CATALOG)
                or errors.get(CONF_POLICY_PATH)
                or "invalid_import"
            )
            return self.async_abort(reason=reason)

        self._base = {
            **import_data,
            CONF_DEVICE_CATALOG: _clean_path(import_data.get(CONF_DEVICE_CATALOG)),
            CONF_POLICY_PATH: _clean_path(import_data.get(CONF_POLICY_PATH)),
        }
        return self.async_create_entry(title="Home Voice Safety Layer", data=dict(self._base))


class HomeVoiceSafetyOptionsFlow(OptionsFlowWithConfigEntry):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        current = {**self.config_entry.data, **self.config_entry.options}
        schema = _options_schema(current)
        if user_input is None:
            return self.async_show_form(step_id="init", data_schema=schema)

        errors = await _validate_input(self.hass, {**current, **user_input})
        if errors:
            return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
        return self.async_create_entry(title="", data=user_input)


def _notify_pending_options(hass, pending: int) -> None:
    message = (
        f"检测到 {pending} 条待确认动作，已暂缓应用新设置。"
        "请先确认或取消这些动作，然后重新保存选项，以免确认令牌丢失。"
    )
    _LOGGER.warning("待确认动作 %s 条，暂缓 options reload：%s", pending, message)
    try:
        from homeassistant.components import persistent_notification
    except ImportError:  # pragma: no cover - HA always provides it at runtime
        return
    persistent_notification.async_create(
        hass,
        message,
        title="Home Voice Safety Layer",
        notification_id=f"{DOMAIN}_options_pending",
    )


async def async_update_options(hass, entry: ConfigEntry) -> None:
    """Reload the entry so policy/catalog/provider changes actually take effect."""
    coordinator = (hass.data.get(DOMAIN) or {}).get(entry.entry_id)
    pending = len(coordinator.engine.gate) if coordinator is not None else 0
    if pending:
        # Do not silently drop parked confirmations: keep the old engine alive
        # and tell the user to finish/cancel them before saving options again.
        _notify_pending_options(hass, pending)
        return
    await hass.config_entries.async_reload(entry.entry_id)