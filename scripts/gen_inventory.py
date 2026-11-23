"""Generate ``devices/inventory.md`` from ``devices/devices.json`` (A6).

Deterministic on purpose: no timestamp, stable ordering, so ``tests/test_inventory.py``
can assert the checked-in file equals a fresh render and catch any drift between the
catalog and the inventory a reader trusts.

    python scripts/gen_inventory.py            # 写入 devices/inventory.md
    python scripts/gen_inventory.py --check    # 只校验，不写文件（CI 用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hvsl.config import Settings  # noqa: E402
from hvsl.models import DeviceEntity, Provenance  # noqa: E402
from hvsl.risk import RiskPolicy  # noqa: E402
from hvsl.tools import build_default_registry  # noqa: E402

CATALOG = ROOT / "devices" / "devices.json"
TARGET = ROOT / "devices" / "inventory.md"


def real_entities_from_settings() -> set[str]:
    """真实设备的唯一来源：CLI、``--check`` 与测试都从这里取。

    读不到配置时按 0 台真实设备处理：少报真实设备，好过把模拟设备误标成真实。
    """
    try:
        settings = Settings.load()
    except Exception:  # .env 不合法时不要让清单生成器整体崩溃
        return set()
    raw = getattr(settings, "ha_real_entities", "") or ""
    return {item.strip() for item in str(raw).split(",") if item.strip()}


def render(
    catalog_path: Path = CATALOG,
    policy_path: Path | None = None,
    real_entities: set[str] | None = None,
) -> str:
    """Render the inventory from ``devices/devices.json``.

    ``real_entities`` makes the declaration source explicit, which is what stops
    ``--check`` and ``pytest`` from reaching opposite conclusions:

    * ``None`` -> read ``HA_REAL_ENTITIES`` from Settings (the CLI default);
    * a set   -> use that fixed set (tests pass ``set()`` so the local ``.env``
      cannot flip their verdict).

    The catalog stays the single source of truth for provenance; a declaration
    that contradicts it raises instead of silently relabelling a device.
    """
    declared = real_entities_from_settings() if real_entities is None else set(real_entities)
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    devices = data["devices"]
    policy = RiskPolicy.load(policy_path or ROOT / "hvsl" / "policy" / "risk_policy.json")
    registry = build_default_registry()

    rows: list[list[str]] = []
    real_count = 0
    danger_count = 0
    for spec in devices:
        entity_id = spec["entity_id"]
        domain = entity_id.split(".", 1)[0]
        tags = list(spec.get("tags", []))
        provenance = spec.get("provenance", "simulated")
        if entity_id in declared and provenance != "real":
            raise ValueError(
                f"{entity_id} 在 HA_REAL_ENTITIES 中声明为真实设备，但 devices/devices.json "
                f"标为 {provenance}；设备清单是 provenance 的唯一真源，"
                "请先经安全评审更新清单，不要用环境变量覆盖"
            )
        if provenance == "real":
            real_count += 1

        classes: list[str] = []
        entity = DeviceEntity(
            entity_id=entity_id,
            domain=domain,
            display_name=spec["display_name"],
            provenance=Provenance(provenance),
            state=spec.get("state", "off"),
            tags=tags,
        )
        for tool in registry.all():
            if tool.ha_service[0] != domain:
                continue
            risk, risk_class, _reason = policy.classify(tool.name, [entity])
            if risk_class and risk.value != "low":
                classes.append(f"{risk_class}({risk.value})")
        if classes:
            danger_count += 1

        rows.append(
            [
                entity_id,
                spec["display_name"],
                domain,
                "真实" if provenance == "real" else "模拟",
                spec.get("state", "off"),
                "、".join(t for t in tags if t.startswith("danger:")) or "-",
                "；".join(sorted(set(classes))) or "-",
            ]
        )

    total = len(devices)
    lines = [
        "# 设备清单（真实 / 模拟）",
        "",
        "> 本文件由 `scripts/gen_inventory.py` 从 `devices/devices.json` 生成，不要手改。",
        "> 重新生成：`python scripts/gen_inventory.py`；校验：`python scripts/gen_inventory.py --check`",
        "",
        f"- 受控实体总数：**{total}**",
        f"- 真实设备：**{real_count}**",
        f"- 模拟实体：**{total - real_count}**",
        f"- 其中带危险标签的实体：**{danger_count}**",
        "",
        "危险标签与 `provenance` 只在安全评审后修改：风险判定读取的是这份清单，"
        "不是设备自报的属性，所以设备无法把自己降级成“安全”。",
        "",
        "| entity_id | 名称 | 域 | 真实/模拟 | 初始状态 | 危险标签 | 命中的危险类别 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    lines += [
        "",
        "## 说明",
        "",
        "- `真实` = 通过本地 Home Assistant 控制的物理设备（Wi-Fi/LAN 本地控制）；"
        "`模拟` = HA 模拟实体或本项目的内置模拟后端，不接任何真实负载。",
        "- 门锁、燃气电磁阀、总电源空开、大功率插座、安防主机一律标注 `danger:*`，"
        "并在演示时保持为模拟实体（方案 R6：危险动作只在虚拟实体上演示）。",
        "- 若 `真实设备` 为 0，则本轮**没有真实设备参与**，README 不得暗示有。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从设备清单生成 inventory.md")
    parser.add_argument("--check", action="store_true", help="只比对，不写入")
    parser.add_argument("--out", default=str(TARGET))
    parser.add_argument(
        "--real-entities",
        default=None,
        help="真实设备 entity_id（逗号分隔）；默认读 Settings（.env / 环境变量）",
    )
    args = parser.parse_args(argv)

    if args.real_entities is None:
        real = real_entities_from_settings()
    else:
        real = {item.strip() for item in args.real_entities.split(",") if item.strip()}
    text = render(real_entities=real)
    out = Path(args.out)
    if args.check:
        current = out.read_text(encoding="utf-8") if out.is_file() else ""
        if current != text:
            print(f"{out} 与 devices/devices.json 不一致，请运行 python scripts/gen_inventory.py", file=sys.stderr)
            return 1
        print(f"{out} 与清单一致")
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"已写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
