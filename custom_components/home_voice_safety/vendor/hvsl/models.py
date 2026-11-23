"""Typed domain model for the decision chain (F-006 ... F-009)."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: P0-6：给 TTS / 通知用的口语化回复上限（字符）。
SPOKEN_MESSAGE_LIMIT = 120

#: 技术调用串（``confirm(token='...')``）绝不允许出现在 spoken_message 里。
_TECHNICAL_CONFIRM_RE = re.compile(r"confirm\s*\(\s*token\s*=\s*['\"][^'\"]*['\"]\s*\)", re.IGNORECASE)
_TOKEN_ASSIGNMENT_RE = re.compile(r"\btoken\s*[=:]\s*['\"]?[0-9a-zA-Z_-]{4,}['\"]?", re.IGNORECASE)


def _to_spoken(text: str) -> str:
    """Strip technical confirmation syntax and cap the length for TTS."""
    cleaned = _TECHNICAL_CONFIRM_RE.sub("", text or "")
    cleaned = _TOKEN_ASSIGNMENT_RE.sub("", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    if len(cleaned) > SPOKEN_MESSAGE_LIMIT:
        cleaned = cleaned[:SPOKEN_MESSAGE_LIMIT].rstrip("，。；、,; ")
    return cleaned


def token_hash(token: str) -> str:
    """Short, non-reversible fingerprint used wherever a token may be logged."""
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()[:12]


class RiskLevel(str, Enum):
    LOW = "low"
    CONFIRM = "confirm"
    DENY = "deny"


class DecisionKind(str, Enum):
    EXECUTED = "executed"
    PARTIAL = "partial"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CLARIFICATION_REQUIRED = "clarification_required"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    DEDUPLICATED = "deduplicated"


class ConfirmationState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Provenance(str, Enum):
    REAL = "real"
    SIMULATED = "simulated"


class DeviceEntity(BaseModel):
    """A controllable entity, either a real LAN device surfaced through HA or a mock."""

    entity_id: str
    domain: str
    display_name: str
    provenance: Provenance = Provenance.SIMULATED
    state: str = "off"
    tags: list[str] = Field(default_factory=list)
    supported_features: list[str] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    #: F1：是否出现在已评审的 ``devices/devices.json`` 清单中。
    #: catalog / simulated 实体默认 True；HA 动态发现但清单未收录的实体必须是 False，
    #: 否则未评审的燃气阀/大功率插座会以 ``low`` 直接下发。
    reviewed: bool = True

    @property
    def is_danger_tagged(self) -> bool:
        return any(t.startswith("danger:") for t in self.tags)


class ToolCall(BaseModel):
    """One namespaced tool invocation - the unit the safety gate evaluates."""

    model_config = ConfigDict(extra="forbid")

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)

    def canonical(self) -> str:
        return json.dumps({"tool": self.tool, "args": self.args}, sort_keys=True, ensure_ascii=False)


class PlannedCall(BaseModel):
    """What the parser produces: a tool plus a *phrase* describing the target.

    The target stays a phrase until :mod:`hvsl.resolver` turns it into concrete
    entity ids, because the ambiguity and no-match cases are what drive the
    clarify branch.
    """

    model_config = ConfigDict(extra="forbid")

    tool: str
    target: str = Field(default="", description="Chinese phrase naming the target device(s)")
    all: bool = Field(default=False, description="Apply to every device matched in the room/scope")
    args: dict[str, Any] = Field(default_factory=dict)

    def fan_out(self) -> bool:
        """May this step hit several entities? Derived from the phrase, not only the parser flag.

        A model that sets ``all=true`` for everything is not trusted: the scope word has to be
        in the utterance (enforced by :func:`hvsl.guards.clamp_plan`) or in this very step's
        target phrase.
        """
        from .guards import SCOPE_WORDS

        return bool(self.all) or any(word in (self.target or "") for word in SCOPE_WORDS)


class Intent(BaseModel):
    """Parsed intent: an ordered plan of tool calls."""

    utterance: str
    name: str = "device_command"
    steps: list[PlannedCall] = Field(default_factory=list)
    provider: str = "rule"
    degraded: bool = False
    confidence: float = 1.0
    notes: list[str] = Field(default_factory=list)


class ResolvedStep(BaseModel):
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    targets: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    risk_class: str = ""
    reason: str = ""


class StepOutcome(BaseModel):
    tool: str
    targets: list[str] = Field(default_factory=list)
    dispatched: bool = False
    state: Literal["executed", "failed", "suppressed", "skipped", "in_flight_unknown"] = "skipped"
    error: str = ""
    latency_ms: float | None = None
    idempotency_key: str = ""


class PendingAction(BaseModel):
    """F-007: a plan parked until the user confirms it."""

    token: str
    session_id: str
    utterance: str
    steps: list[ResolvedStep]
    risk_class: str
    risk_level: RiskLevel = RiskLevel.CONFIRM
    created_at: float
    expires_at: float
    consumed: bool = False
    trace_id: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at

    def summary(self) -> str:
        targets = ", ".join(t for step in self.steps for t in step.targets)
        return f"{self.utterance} -> {targets} ({self.risk_class})"


class Decision(BaseModel):
    """The answer returned to the caller, and the unit the evaluator scores."""

    trace_id: str
    kind: DecisionKind
    message: str
    #: P0-6：面向 TTS / 通知的口语化回复。无 token、无 ``confirm(...)``、≤120 字。
    #: ``confirmation_token`` 仍然单独给程序用，绝不混进这句话。
    spoken_message: str = ""
    utterance: str
    session_id: str = "default"
    intent: str = ""
    steps: list[ResolvedStep] = Field(default_factory=list)
    outcomes: list[StepOutcome] = Field(default_factory=list)
    confirmation_token: str | None = None
    candidates: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.LOW
    dispatch_count: int = 0
    nlu_provider: str = "rule"
    degraded: bool = False
    timings_ms: dict[str, float] = Field(default_factory=dict)
    ts: float = Field(default_factory=time.time)

    @model_validator(mode="after")
    def _derive_spoken_message(self) -> "Decision":
        """Never let a raw technical string reach TTS or a notification body."""
        self.spoken_message = _to_spoken(self.spoken_message or self.message)
        return self

    @staticmethod
    def new_trace_id() -> str:
        return uuid.uuid4().hex[:12]

    def as_dict(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())


def idempotency_key(session_id: str, tool: str, args: dict[str, Any], targets: list[str]) -> str:
    """Stable key for F-008: same intent, same targets, same params -> same key.

    Deliberately excludes the wall clock so that replays inside the dedup window
    collapse onto one row.
    """
    payload = json.dumps(
        {"session": session_id, "tool": tool, "args": args, "targets": sorted(targets)},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
