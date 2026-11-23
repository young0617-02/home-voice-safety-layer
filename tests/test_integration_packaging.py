"""Packaging regression (P0-1): the integration must import without ``pip install hvsl``.

HACS users copy ``custom_components/home_voice_safety/`` only.  The vendored
``hvsl``/``pipeline`` copy and the ``sys.path`` injection in the package
``__init__.py`` are therefore part of the install contract, not a dev convenience.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "home_voice_safety"
VENDOR = INTEGRATION / "vendor"
SYNC_SCRIPT = ROOT / "scripts" / "sync_vendor.py"
MANIFEST = VENDOR / "MANIFEST.sha256"


def _run_python(code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_coordinator_imports_with_vendored_core_only(tmp_path: Path) -> None:
    """Import the HA coordinator in a child process that starts without hvsl loaded.

    Home Assistant itself is not a dev dependency of this repo, so the child gets
    three tiny stubs for the HA classes used at module import time.  The assertion
    we care about is that ``hvsl``/``pipeline`` resolve inside ``vendor/`` and that
    the coordinator imports through the normal ``custom_components.*`` path.
    """
    code = f"""
import sys
import types
from pathlib import Path

sys.dont_write_bytecode = True

# Root is added only so the ``custom_components`` namespace package is importable.
# No ``hvsl``/``pipeline`` path is added here: the integration must do that itself.
sys.path.insert(0, {str(ROOT)!r})
for name in [
    n for n in list(sys.modules)
    if n == "hvsl" or n.startswith("hvsl.")
    or n == "pipeline" or n.startswith("pipeline.")
]:
    sys.modules.pop(name, None)

ha = types.ModuleType("homeassistant")

class _Generic:
    @classmethod
    def __class_getitem__(cls, _item):
        return cls

class ConfigEntry(_Generic):
    pass

class HomeAssistant(_Generic):
    pass

class DataUpdateCoordinator(_Generic):
    def __init__(self, *args, **kwargs):
        pass

class UpdateFailed(Exception):
    pass

config_entries = types.ModuleType("homeassistant.config_entries")
config_entries.ConfigEntry = ConfigEntry
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = HomeAssistant
helpers = types.ModuleType("homeassistant.helpers")
update_coordinator = types.ModuleType("homeassistant.helpers.update_coordinator")
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator
update_coordinator.UpdateFailed = UpdateFailed
ha.config_entries = config_entries
ha.core = core
ha.helpers = helpers
helpers.update_coordinator = update_coordinator
sys.modules.update(
    {{
        "homeassistant": ha,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.update_coordinator": update_coordinator,
    }}
)

import custom_components.home_voice_safety.coordinator as coordinator
import hvsl
import pipeline

vendor = Path({str(VENDOR)!r}).resolve()
hvsl_file = Path(hvsl.__file__).resolve()
pipeline_file = Path(pipeline.__file__).resolve()
assert hvsl_file.is_relative_to(vendor), f"hvsl resolved outside vendor: {{hvsl_file}}"
assert pipeline_file.is_relative_to(vendor), f"pipeline resolved outside vendor: {{pipeline_file}}"
assert coordinator.BUNDLED_POLICY
print("hvsl=" + str(hvsl_file))
print("pipeline=" + str(pipeline_file))
print("coordinator=" + coordinator.__file__)
"""
    result = _run_python(code, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hvsl=" in result.stdout
    assert "pipeline=" in result.stdout
    assert str(VENDOR) in result.stdout


def test_vendor_manifest_matches_files_and_source() -> None:
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "vendor check OK" in result.stdout

    entries: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        entries[relative] = digest

    assert "hvsl/policy/risk_policy.json" in entries
    for relative, expected in entries.items():
        path = VENDOR / relative
        assert path.is_file(), f"manifest lists a missing file: {relative}"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, relative


def test_vendored_policy_is_loadable() -> None:
    from hvsl.risk import RiskPolicy

    policy = RiskPolicy.load(VENDOR / "hvsl" / "policy" / "risk_policy.json")
    assert policy.danger_classes(), "vendored risk policy has no danger classes"


def test_vendor_manifest_has_no_cache_artefacts() -> None:
    """The vendored *source* must never include caches.

    Runtime imports may create ``__pycache__`` next to the vendored modules; that
    is normal Python behaviour and ``scripts/sync_vendor.py`` prunes it on the next
    sync.  What must never happen is shipping cache files as part of the copy.
    """
    listed = [line.split("  ", 1)[1] for line in MANIFEST.read_text(encoding="utf-8").splitlines() if line]
    assert listed
    assert not [path for path in listed if "__pycache__" in path or path.endswith(".pyc")]


def test_manifest_and_hacs_metadata() -> None:
    manifest = json.loads((INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "0.2.0"
    assert manifest["iot_class"] == "local_polling"
    assert "homeassistant" not in manifest, "homeassistant 属于 hacs.json"

    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    assert hacs["name"] == "Home Voice Safety Layer"
    assert hacs["render_readme"] is True
    assert hacs["homeassistant"]