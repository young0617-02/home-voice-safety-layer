"""F-006 tool layer: namespaced tool names, JSON Schema constrained arguments.

HA 2026.9 forces integration prefixes on tool names, so the registry refuses to
register a bare name. Argument models are Pydantic; the published JSON Schema is
generated from them, which keeps the schema and the validation in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Type

from pydantic import BaseModel, ConfigDict, Field

from . import TOOL_NAMESPACE
from .errors import NamespaceViolationError, ToolValidationError, UnknownToolError


class ToolArgs(BaseModel):
    """Common shape: every tool acts on at least one resolved entity."""

    model_config = ConfigDict(extra="forbid")

    entity_ids: list[str] = Field(min_length=1, description="Resolved HA entity ids")


class NoArgs(ToolArgs):
    pass


class BrightnessArgs(ToolArgs):
    brightness_pct: int | None = Field(default=None, ge=1, le=100)


class TemperatureArgs(ToolArgs):
    temperature_c: float = Field(ge=5, le=40)


class ModeArgs(ToolArgs):
    mode: str


class PercentageArgs(ToolArgs):
    percentage: int = Field(ge=0, le=100)


class VolumeArgs(ToolArgs):
    volume_level: int = Field(ge=0, le=100, description="Percent, 0-100")


class DeltaArgs(ToolArgs):
    """Relative change in percentage points.

    The backend reads the current state and converts this delta into an absolute
    ``brightness_pct`` / ``volume_level`` before dispatching.  The bound is part of
    the safety contract: a single spoken command may not move a device by more
    than half of its range, and the backend never widens it.
    """

    delta_pct: int = Field(ge=-50, le=50, description="Relative percentage change")


class CodeArgs(ToolArgs):
    code: str | None = Field(default=None, max_length=16)


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    args_model: Type[BaseModel]
    ha_service: tuple[str, str]
    idempotent: bool = True
    tags: tuple[str, ...] = field(default_factory=tuple)

    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("$defs", None)
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": {
                "type": "object",
                "properties": schema.get("properties", {}),
                "required": schema.get("required", ["entity_ids"]),
                "additionalProperties": False,
            },
            "ha_service": {"domain": self.ha_service[0], "service": self.ha_service[1]},
            "idempotent": self.idempotent,
        }


class ToolRegistry:
    def __init__(self, namespace: str = TOOL_NAMESPACE) -> None:
        self.namespace = namespace
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> ToolDef:
        if not tool.name.startswith(f"{self.namespace}."):
            raise NamespaceViolationError(
                f"tool name {tool.name!r} must start with the integration namespace {self.namespace!r}"
            )
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool name {tool.name!r}")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> ToolDef:
        try:
            return self._tools[name]
        except KeyError:
            raise UnknownToolError(f"unknown tool {name!r}; registered: {sorted(self._tools)}") from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[ToolDef]:
        return [self._tools[k] for k in sorted(self._tools)]

    def validate_args(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Pydantic-validate arguments; anything unknown or malformed is rejected."""
        tool = self.get(name)
        try:
            model = tool.args_model.model_validate(args)
        except Exception as exc:  # pydantic ValidationError
            raise ToolValidationError(f"tool {name} argument error: {exc}") from exc
        return model.model_dump(exclude_none=True)

    def json_schemas(self) -> list[dict[str, Any]]:
        return [tool.json_schema() for tool in self.all()]

    def by_ha_service(self, domain: str, service: str) -> ToolDef | None:
        for tool in self._tools.values():
            if tool.ha_service == (domain, service):
                return tool
        return None


def build_default_registry() -> ToolRegistry:
    """The whole tool surface the assistant may use. Anything absent is rejected."""
    reg = ToolRegistry()
    add = reg.register

    # lights
    add(ToolDef("hvs.light.turn_on", "Turn a light on, optionally setting brightness", BrightnessArgs, ("light", "turn_on")))
    add(ToolDef("hvs.light.turn_off", "Turn a light off", NoArgs, ("light", "turn_off")))
    add(ToolDef("hvs.light.set_brightness", "Set light brightness percentage", BrightnessArgs, ("light", "turn_on")))
    add(
        ToolDef(
            "hvs.light.adjust_brightness",
            "Adjust light brightness by a relative percentage against the current state",
            DeltaArgs,
            ("light", "turn_on"),
            idempotent=False,
        )
    )

    # generic switches (sockets, relays)
    add(ToolDef("hvs.switch.turn_on", "Turn a switch/socket on", NoArgs, ("switch", "turn_on")))
    add(ToolDef("hvs.switch.turn_off", "Turn a switch/socket off", NoArgs, ("switch", "turn_off")))

    # locks - unlock is dangerous by policy
    add(ToolDef("hvs.lock.lock", "Lock the door lock", CodeArgs, ("lock", "lock")))
    add(ToolDef("hvs.lock.unlock", "Unlock the door lock", CodeArgs, ("lock", "unlock")))

    # climate
    add(ToolDef("hvs.climate.set_temperature", "Set the target temperature", TemperatureArgs, ("climate", "set_temperature")))
    add(ToolDef("hvs.climate.set_hvac_mode", "Set the climate operating mode", ModeArgs, ("climate", "set_hvac_mode")))
    add(ToolDef("hvs.climate.turn_on", "Turn the climate on", NoArgs, ("climate", "turn_on")))
    add(ToolDef("hvs.climate.turn_off", "Turn the climate off", NoArgs, ("climate", "turn_off")))

    # covers / curtains
    add(ToolDef("hvs.cover.open", "Open a cover / curtain", NoArgs, ("cover", "open_cover")))
    add(ToolDef("hvs.cover.close", "Close a cover / curtain", NoArgs, ("cover", "close_cover")))
    add(ToolDef("hvs.cover.set_position", "Set cover position percentage", PercentageArgs, ("cover", "set_cover_position")))

    # alarm panel
    add(ToolDef("hvs.alarm.arm_away", "Arm the alarm panel in away mode", CodeArgs, ("alarm_control_panel", "alarm_arm_away")))
    add(ToolDef("hvs.alarm.arm_home", "Arm the alarm panel in home mode", CodeArgs, ("alarm_control_panel", "alarm_arm_home")))
    add(ToolDef("hvs.alarm.disarm", "Disarm the alarm panel", CodeArgs, ("alarm_control_panel", "alarm_disarm")))

    # fan / air
    add(ToolDef("hvs.fan.turn_on", "Turn a fan on", NoArgs, ("fan", "turn_on")))
    add(ToolDef("hvs.fan.turn_off", "Turn a fan off", NoArgs, ("fan", "turn_off")))
    add(ToolDef("hvs.fan.set_percentage", "Set fan speed percentage", PercentageArgs, ("fan", "set_percentage")))

    # vacuum
    add(ToolDef("hvs.vacuum.start", "Start the robot vacuum", NoArgs, ("vacuum", "start")))
    add(ToolDef("hvs.vacuum.stop", "Stop the robot vacuum where it is", NoArgs, ("vacuum", "stop"), idempotent=False))
    add(ToolDef("hvs.vacuum.return_home", "Send the robot vacuum back to its dock", NoArgs, ("vacuum", "return_to_base")))

    # media
    add(ToolDef("hvs.media_player.turn_on", "Turn the media player on", NoArgs, ("media_player", "turn_on")))
    add(ToolDef("hvs.media_player.volume_set", "Set the media player volume", VolumeArgs, ("media_player", "volume_set")))
    add(
        ToolDef(
            "hvs.media_player.adjust_volume",
            "Adjust the media player volume by a relative percentage against the current state",
            DeltaArgs,
            ("media_player", "volume_set"),
            idempotent=False,
        )
    )
    add(ToolDef("hvs.media_player.turn_off", "Turn the media player off", NoArgs, ("media_player", "turn_off")))

    return reg
