"""F-007 risk classification: a data-driven whitelist of dangerous action classes.

Anything the policy marks ``confirm`` must be parked behind an explicit user
confirmation; ``deny`` is refused outright. Both decisions are made here, in one
pure function over declarative policy, so the red line can be audited and mutated
in a single place.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import PolicyError
from .models import DeviceEntity, RiskLevel

_SEVERITY = {RiskLevel.LOW: 0, RiskLevel.CONFIRM: 1, RiskLevel.DENY: 2}

#: F1 兜底：未评审实体上这些域的动作绝不允许沿用 ``default_risk=low``。
_UNREVIEWED_DANGER_DOMAINS = frozenset(
    {"lock", "switch", "cover", "alarm_control_panel", "vacuum", "climate"}
)
UNREVIEWED_RISK_CLASS = "unreviewed_entity"
_UNREVIEWED_REASON = (
    "目标实体不在已评审的设备清单（catalog）中，危险域动作默认不直接下发，"
    "需人工确认（unreviewed entity）"
)
_UNREVIEWED_PROMPT = (
    "【{{targets}}】未经过安全评审（unreviewed entity），该动作属于危险域。"
    "请回复确认（confirm）后才会下发。"
)


class RiskRule:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.id: str = spec["id"]
        self.risk = RiskLevel(spec["risk"])
        self.reason: str = spec.get("reason", "")
        self.prompt: str = spec.get("prompt", "")
        match = spec.get("match", {})
        self.tools_any: list[str] = list(match.get("tools_any", []))
        self.tool_prefix_any: list[str] = list(match.get("tool_prefix_any", []))
        self.domain_any: list[str] = list(match.get("domain_any", []))
        self.tag_any: list[str] = list(match.get("tag_any", []))

    def matches(self, tool: str, entities: list[DeviceEntity]) -> bool:
        if self.tools_any and tool not in self.tools_any:
            return False
        if self.tool_prefix_any and not any(tool.startswith(p) for p in self.tool_prefix_any):
            return False
        if self.domain_any and not any(e.domain in self.domain_any for e in entities):
            return False
        if self.tag_any:
            entity_tags = {tag for e in entities for tag in e.tags}
            if not entity_tags.intersection(self.tag_any):
                return False
        # A rule with only tool constraints still needs the tools clause; a rule
        # with entity constraints needs at least one matching entity (checked above).
        return bool(self.tools_any or self.tool_prefix_any)


class RefusalRule:
    def __init__(self, spec: dict[str, Any]) -> None:
        self.id = spec["id"]
        self.patterns = list(spec.get("patterns", []))
        self.reason = spec.get("reason", "")


class RiskPolicy:
    def __init__(
        self,
        rules: list[RiskRule],
        refusals: list[RefusalRule],
        default_risk: RiskLevel,
        unreviewed_risk: RiskLevel = RiskLevel.CONFIRM,
    ):
        self.rules = rules
        self.refusals = refusals
        self.default_risk = default_risk
        #: F1：未评审实体落在危险域时至少升到这一档，只能是 confirm / deny。
        self.unreviewed_risk = unreviewed_risk

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RiskPolicy:
        rules = [RiskRule(r) for r in data.get("rules", [])]
        if not rules:
            raise PolicyError("risk policy must declare at least one rule")
        seen = {r.id for r in rules}
        if len(seen) != len(rules):
            raise PolicyError("risk policy rule ids must be unique")
        # 缺失时默认 confirm（向后兼容旧策略文件）；显式配置 low 会让未评审的
        # 危险设备重新变成“无确认直接下发”，必须在加载期就拒绝。
        try:
            unreviewed_risk = RiskLevel(data.get("unreviewed_risk", "confirm"))
        except ValueError as exc:
            raise PolicyError(f"unreviewed_risk 必须是 confirm 或 deny，收到 {data.get('unreviewed_risk')!r}") from exc
        if unreviewed_risk is RiskLevel.LOW:
            raise PolicyError("unreviewed_risk 不能是 low：未评审实体绝不允许默认直接下发")
        return cls(
            rules,
            [RefusalRule(r) for r in data.get("refusals", [])],
            RiskLevel(data.get("default_risk", "low")),
            unreviewed_risk,
        )

    @classmethod
    def load(cls, path: str | Path) -> RiskPolicy:
        raw = Path(path).read_text(encoding="utf-8")
        return cls.from_dict(json.loads(raw))

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_risk": self.default_risk.value,
            "unreviewed_risk": self.unreviewed_risk.value,
            "rules": [
                {
                    "id": r.id,
                    "risk": r.risk.value,
                    "match": {
                        "tools_any": r.tools_any,
                        "tool_prefix_any": r.tool_prefix_any,
                        "domain_any": r.domain_any,
                        "tag_any": r.tag_any,
                    },
                    "reason": r.reason,
                    "prompt": r.prompt,
                }
                for r in self.rules
            ],
            "refusals": [{"id": r.id, "patterns": r.patterns, "reason": r.reason} for r in self.refusals],
        }

    def classify(self, tool: str, entities: list[DeviceEntity]) -> tuple[RiskLevel, str, str]:
        """Return (risk, class_id, reason) for one tool call against its targets.

        F1 兜底放在规则结果之后、返回之前：无论计划来自规则解析、LLM 还是
        cross_check 纠偏，最终都要经过这里，未评审实体的危险域动作无法绕过。
        """
        hits = [rule for rule in self.rules if rule.matches(tool, entities)]
        if not hits:
            risk, risk_class, reason = self.default_risk, "", ""
        else:
            top = max(hits, key=lambda r: _SEVERITY[r.risk])
            risk, risk_class, reason = top.risk, top.id, top.reason
        return self._apply_unreviewed_floor(tool, entities, risk, risk_class, reason)

    def _apply_unreviewed_floor(
        self,
        tool: str,
        entities: list[DeviceEntity],
        risk: RiskLevel,
        risk_class: str,
        reason: str,
    ) -> tuple[RiskLevel, str, str]:
        """把未评审实体在危险域上的风险抬到 ``unreviewed_risk``（只能升不能降）。"""
        if not any(not entity.reviewed for entity in entities):
            return risk, risk_class, reason
        if not _touches_unreviewed_danger_domain(tool, entities):
            return risk, risk_class, reason
        if _SEVERITY[self.unreviewed_risk] <= _SEVERITY[risk]:
            # 已有规则给出同级或更高的风险时保持原判定（例如 danger:gas -> deny）。
            return risk, risk_class, reason
        return self.unreviewed_risk, UNREVIEWED_RISK_CLASS, _UNREVIEWED_REASON

    def refusal_for(self, utterance: str) -> RefusalRule | None:
        for rule in self.refusals:
            if any(pattern in utterance for pattern in rule.patterns):
                return rule
        return None

    def danger_classes(self) -> list[str]:
        return sorted({r.id for r in self.rules if r.risk is not RiskLevel.LOW})

    def prompt_for(self, risk_class: str) -> str:
        if risk_class == UNREVIEWED_RISK_CLASS:
            return _UNREVIEWED_PROMPT
        for rule in self.rules:
            if rule.id == risk_class:
                return rule.prompt or rule.reason
        return "该动作属于危险操作，请确认后才会执行。"


def _tool_domain(tool: str) -> str:
    """``hvs.switch.turn_on`` -> ``switch``（未知格式返回空串）。"""
    parts = tool.split(".")
    return parts[1] if len(parts) >= 3 else ""


def _touches_unreviewed_danger_domain(tool: str, entities: list[DeviceEntity]) -> bool:
    """工具域或任一目标实体域落在未评审兜底的危险域内。"""
    if _tool_domain(tool) in _UNREVIEWED_DANGER_DOMAINS:
        return True
    return any(entity.domain in _UNREVIEWED_DANGER_DOMAINS for entity in entities)


def combine(*levels: RiskLevel) -> RiskLevel:
    if not levels:
        return RiskLevel.LOW
    return max(levels, key=lambda level: _SEVERITY[level])
