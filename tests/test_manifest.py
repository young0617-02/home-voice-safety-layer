"""P1-5 release metadata checks for the Home Assistant integration."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.home_voice_safety import diagnostics
from custom_components.home_voice_safety.const import CONFIG_ENTRY_VERSION, INTEGRATION_VERSION

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "home_voice_safety"
MANIFEST = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
HACS = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
QUALITY_SCALES = {"bronze", "silver", "gold", "platinum"}


def test_manifest_required_release_keys() -> None:
    assert MANIFEST["domain"] == "home_voice_safety"
    assert MANIFEST["config_flow"] is True
    assert MANIFEST["version"] == "0.2.0"
    assert MANIFEST["iot_class"] == "local_polling"
    assert "homeassistant" not in MANIFEST, "homeassistant 属于 hacs.json，不是 manifest schema"
    assert HACS["homeassistant"] == "2025.1.0", (
        "HA 2024.6 自带 pydantic 1.10，无法满足 pydantic>=2.5；最低版本改为 2025.1"
    )
    assert MANIFEST["integration_type"] == "hub"
    assert {"pydantic", "httpx"} <= {
        requirement.split(">=", 1)[0].split("==", 1)[0] for requirement in MANIFEST["requirements"]
    }


def test_manifest_release_metadata_is_honest() -> None:
    assert MANIFEST["quality_scale"] in QUALITY_SCALES
    assert MANIFEST["loggers"] == ["custom_components.home_voice_safety"]
    assert isinstance(MANIFEST["codeowners"], list)
    assert MANIFEST["codeowners"], "codeowners 不能是空列表"
    assert all(owner.startswith("@") and "【填" not in owner for owner in MANIFEST["codeowners"]), (
        "codeowners 必须是真实 GitHub 账号（@handle）；未确定 owner 时才允许保留【填：】占位"
    )


def test_documentation_points_to_the_same_real_repo() -> None:
    owner = MANIFEST["codeowners"][0][1:]
    repo = f"https://github.com/{owner}/home-voice-safety-layer"
    assert MANIFEST["documentation"] == repo, "documentation 必须与 codeowners 指向同一仓库"
    assert MANIFEST["issue_tracker"] == repo + "/issues", "issue_tracker 必须与 codeowners 指向同一仓库"


def test_versions_are_in_sync_and_migration_target_is_two() -> None:
    assert CONFIG_ENTRY_VERSION == 2
    assert INTEGRATION_VERSION == MANIFEST["version"]


def test_brand_and_diagnostics_surface_exists() -> None:
    assert (INTEGRATION / "brand" / "README.md").is_file()
    assert callable(diagnostics.async_get_config_entry_diagnostics)


def test_sensor_device_info_does_not_touch_privacy_attributes() -> None:
    source = (INTEGRATION / "sensor.py").read_text(encoding="utf-8")
    assert "_attr_device_info" in source
    assert "manufacturer=\"Home Voice Safety\"" in source
    assert "sw_version=INTEGRATION_VERSION" in source
    # P0-4 privacy contract must stay intact.
    assert "_unrecorded_attributes = frozenset({\"actions\"})" in source
    assert '"session_alias"' in source