"""A6: the real/simulated inventory must be generated, complete and drift-free."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import gen_inventory  # noqa: E402

CATALOG_PATH = ROOT / "devices" / "devices.json"
INVENTORY = ROOT / "devices" / "inventory.md"
# 显式固定值：测试不读本地 .env（HA_REAL_ENTITIES）。
# provenance 的唯一真源是 devices/devices.json；--check 默认读 Settings，
# 但声明与清单冲突时会 raise，而不是把模拟设备静默标成真实。
REAL: set[str] = set()


def catalog():
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["devices"]


def effective_real_ids(devices) -> set[str]:
    """渲染时会被标成「真实」的实体：只由清单自报的 provenance 决定。"""
    return {spec["entity_id"] for spec in devices if spec.get("provenance") == "real"}


def test_inventory_file_is_current():
    assert INVENTORY.is_file(), "缺少 devices/inventory.md，请运行 python scripts/gen_inventory.py"
    assert INVENTORY.read_text(encoding="utf-8") == gen_inventory.render(real_entities=REAL), (
        "devices/inventory.md 与 devices/devices.json（或 Settings 的真实设备集合）不一致，请重新生成"
    )


def test_every_device_is_listed_and_labelled():
    text = INVENTORY.read_text(encoding="utf-8")
    devices = catalog()
    for spec in devices:
        assert spec["entity_id"] in text
        assert spec["display_name"] in text
    rows = [line for line in text.splitlines() if line.startswith("| ") and "entity_id" not in line and "---" not in line]
    assert len(rows) == len(devices), f"清单表 {len(rows)} 行，实际设备 {len(devices)} 台"
    for row in rows:
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        assert cells[3] in {"真实", "模拟"}, row


def test_totals_match_the_rows():
    text = INVENTORY.read_text(encoding="utf-8")
    devices = catalog()
    real = len(effective_real_ids(devices))
    assert f"- 受控实体总数：**{len(devices)}**" in text
    assert f"- 真实设备：**{real}**" in text
    assert f"- 模拟实体：**{len(devices) - real}**" in text


def test_no_real_devices_says_so_out_loud():
    text = INVENTORY.read_text(encoding="utf-8")
    if not effective_real_ids(catalog()):
        assert "本轮**没有真实设备参与**" in text


def test_danger_entities_are_identified_by_class():
    text = INVENTORY.read_text(encoding="utf-8")
    for danger_class in ("door_unlock", "gas_supply_open", "gas_supply_close", "main_power_off",
                         "main_power_on", "high_power_socket_on", "high_power_socket_off", "alarm_disarm"):
        assert danger_class in text, f"清单里没有标出危险类别 {danger_class}"


def test_dangerous_entities_are_simulated_for_the_demo():
    """方案 R6：门锁/燃气一类只在虚拟实体上演示。"""
    for spec in catalog():
        if any(tag.startswith("danger:") for tag in spec.get("tags", [])):
            assert spec.get("provenance", "simulated") == "simulated", f"{spec['entity_id']} 是真实危险设备，演示前必须改成模拟"


def test_real_entities_cannot_override_the_catalog():
    """HA_REAL_ENTITIES 只能复核清单：声明与 catalog 冲突时拒绝生成，而不是静默标真。"""
    with pytest.raises(ValueError, match="唯一真源"):
        gen_inventory.render(real_entities={"light.living_room_main"})
