"""Loader for the reviewed device catalog (``devices/devices.json``).

Single place where the two safety-relevant artifacts are read:

* per-entity specs (display name, aliases, ``danger:*`` tags, real/simulated), and
* the generic device vocabulary (「灯」「插座」「锁」...) that decides whether a
  spoken class word may be mapped onto this domain at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import DeviceEntity, Provenance


def load_specs(path: str | Path | None) -> list[dict[str, Any]]:
    if not path or not Path(path).is_file():
        return []
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data.get("devices", []))


def load_entities(path: str | Path) -> list[DeviceEntity]:
    entities: list[DeviceEntity] = []
    for spec in load_specs(path):
        entities.append(
            DeviceEntity(
                entity_id=spec["entity_id"],
                domain=spec["entity_id"].split(".", 1)[0],
                display_name=spec["display_name"],
                provenance=Provenance(spec.get("provenance", "simulated")),
                state=spec.get("state", "off"),
                tags=list(spec.get("tags", [])),
                supported_features=list(spec.get("supported_features", [])),
                attributes=dict(spec.get("attributes", {})),
                aliases=list(spec.get("aliases", [])),
                # 能进入这份清单即代表已人工评审（F1）。
                reviewed=True,
            )
        )
    return entities


def load_domain_words(path: str | Path | None) -> dict[str, list[str]]:
    if not path or not Path(path).is_file():
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    words = data.get("domain_words") or {}
    return {domain: list(values) for domain, values in words.items() if not domain.startswith("_")}
