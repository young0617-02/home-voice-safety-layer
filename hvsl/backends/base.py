"""Device backend abstraction (F-003): the decision layer never talks to HA directly."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..errors import PreDispatchError
from ..models import DeviceEntity
from ..tools import ToolDef

#: Domains that support spoken relative adjustments.  Anything else is refused
#: even if a model or a caller fabricates a ``delta_pct`` argument.
ADJUSTABLE_DOMAINS = frozenset({"light", "media_player"})

#: HA stores light brightness as 0-255 and media volume as 0.0-1.0.  The simulated
#: backend deliberately copies those shapes so one conversion rule covers both.
_LIGHT_BRIGHTNESS_SCALE = 2.54
_MEDIA_VOLUME_SCALE = 100.0


def current_percent(entity: DeviceEntity) -> int | None:
    """Read the entity's current brightness/volume as a 0-100 percentage.

    Returns ``None`` when the attribute is absent or not numeric.  Callers must
    treat that as "state unknown" and refuse instead of falling back to a default.
    """
    key = "brightness" if entity.domain == "light" else "volume_level"
    value = entity.attributes.get(key)
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if entity.domain == "light":
        return round(numeric / _LIGHT_BRIGHTNESS_SCALE)
    return round(numeric * _MEDIA_VOLUME_SCALE)


def resolve_adjust_args(entity: DeviceEntity, delta_pct: int) -> dict[str, int]:
    """Turn a relative delta into the absolute arguments the device expects.

    Contract (one rule per line, each covered by ``tests/test_relative_adjust.py``):

    * ``state == "on"`` with a readable brightness/volume -> use that value;
    * ``state == "off"`` and ``delta_pct > 0`` -> base is 0 (the device is
      explicitly off, so starting from zero is a fact, not a guess);
    * ``state == "off"`` and ``delta_pct < 0`` -> :class:`PreDispatchError`, no
      dispatch (there is no lower value to move to);
    * ``state == "on"`` but the attribute is missing, or the state is
      ``unavailable`` / ``unknown`` / anything not understood ->
      :class:`PreDispatchError`, no dispatch.  There is deliberately **no**
      fallback to 50%.
    * light clamps to 1..100, media volume clamps to 0..100.
    """
    # Every branch below is a deterministic refusal: retrying the same intent
    # cannot change the answer, so the engine must not call it "可重试".
    if entity.domain not in ADJUSTABLE_DOMAINS:
        raise PreDispatchError(
            f"「{entity.display_name}」不是可相对调节的设备（仅支持 light / media_player）",
            retryable=False,
        )
    if delta_pct == 0:
        raise PreDispatchError("相对变化为 0", retryable=False)

    state = (entity.state or "").strip().lower()
    label = "亮度" if entity.domain == "light" else "音量"
    if state in {"", "unavailable", "unknown"}:
        raise PreDispatchError(
            f"读不到「{entity.display_name}」的当前状态（{state or 'empty'}），无法做相对调节",
            retryable=False,
        )

    if entity.domain == "media_player":
        # 音量是设备属性，与播放状态解耦。真实 HA 媒体实体几乎从不返回
        # "on"/"off"，而是 "playing" / "paused" / "idle"（真机实测
        # media_player.living_room 即为 "playing"）。只要读得到当前音量就允许
        # 调整；读不到则安全失败，绝不回退到默认值。
        current = current_percent(entity)
        if current is None:
            raise PreDispatchError(
                f"读不到「{entity.display_name}」的当前{label}，无法做相对调节",
                retryable=False,
            )
        base = current
    elif state == "off":
        if delta_pct < 0:
            # Keep the canonical phrase intact; append the device name for a clearer
            # audit/spoken explanation.
            raise PreDispatchError(
                f"设备已关闭，无法调低（「{entity.display_name}」）", retryable=False
            )
        base = 0
    elif state == "on":
        current = current_percent(entity)
        if current is None:
            raise PreDispatchError(
                f"读不到「{entity.display_name}」的当前{label}，无法做相对调节",
                retryable=False,
            )
        base = current
    else:
        raise PreDispatchError(
            f"「{entity.display_name}」当前状态为 {state!r}，无法做相对调节",
            retryable=False,
        )

    target = base + int(delta_pct)
    if entity.domain == "light":
        return {"brightness_pct": max(1, min(100, target))}
    return {"volume_level": max(0, min(100, target))}


class DeviceBackend(ABC):
    """Everything the engine needs from the device world.

    ``call_tool`` is the *only* path that may change physical state, which keeps
    the safety gate and the idempotency guard in front of every dispatch.
    """

    kind: str = "abstract"
    #: 设备泛称词表（来自已评审清单），供 :class:`hvsl.resolver.TargetResolver` 判定类指。
    domain_words: dict[str, list[str]] = {}

    @abstractmethod
    def entities(self) -> list[DeviceEntity]: ...

    @abstractmethod
    def get(self, entity_id: str) -> DeviceEntity | None: ...

    @abstractmethod
    def call_tool(self, tool: ToolDef, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None:
        return None

    def domains(self) -> list[str]:
        return sorted({e.domain for e in self.entities()})
