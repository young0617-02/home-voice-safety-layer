"""Deterministic guards that must hold *whatever* produced the intent.

The LLM is allowed to propose a plan; it is not allowed to decide that an
ambiguous request was actually unambiguous, that a timer should run right now,
or that "所有" was implied. Those three calls are made here, in the engine, so
swapping the parser cannot weaken the safety story.
"""

from __future__ import annotations

import re

from .models import Intent, PlannedCall

ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

# 定时/场景/延后执行：本层只处理即时指令，宁可拒绝也不"现在就执行明天的事"。
UNSUPPORTED_WORDS = (
    "明天", "后天", "今晚", "明晚", "每天", "每周", "定时", "预约", "计划",
    "分钟后", "小时后", "半小时后", "起床", "睡觉前",
)

# 只有句子里真的出现范围词，才允许一个工具调用打到多个实体。
SCOPE_WORDS = ("都", "全部", "所有", "全屋", "整个", "每个", "每一", "统统", "一起")


def unsupported_reason(utterance: str) -> str | None:
    """Longest matching time expression wins so the message quotes the real phrase."""
    hits = [word for word in UNSUPPORTED_WORDS if word in utterance]
    return max(hits, key=len) if hits else None


def scope_requested(utterance: str) -> bool:
    return any(word in utterance for word in SCOPE_WORDS)


def grounded_in(target: str, utterance: str, names_for_id: dict[str, list[str]] | None = None) -> bool:
    """Is the parser's target actually supported by the user's own words?

    Two shapes are accepted:

    * a Chinese phrase - every Chinese character of it must occur in the utterance, so a
      model cannot answer "打开客厅的灯" by picking "客厅大灯" on its own;
    * a catalog entity id - accepted only when the user's words contain that device's
      display name or one of its aliases. Without this check a model can silently
      choose *which* light to touch by returning ``light.living_room_main``.
    """
    if not target:
        return True
    if ENTITY_ID_RE.match(target):
        names = (names_for_id or {}).get(target)
        if names is None:
            return True  # 清单里没有这个 id：交给 resolver 报“无匹配设备”
        return any(name and name in utterance for name in names)
    chars = {ch for ch in target if "一" <= ch <= "鿿"}
    return not chars or all(ch in utterance for ch in chars)


def grounding_veto(
    steps: list[PlannedCall],
    utterance: str,
    fallback_targets: dict[str, str],
    names_for_id: dict[str, list[str]] | None = None,
) -> tuple[list[PlannedCall], list[str]]:
    """Replace invented device names with what the deterministic parser actually heard.

    ``fallback_targets`` maps tool -> target phrase from the shadow (rule) parse.
    A tool the rule parser never produced a target for becomes an empty target, which
    forces the resolver to ask instead of executing the model's guess.
    """
    vetoed: list[PlannedCall] = []
    notes: list[str] = []
    for step in steps:
        if grounded_in(step.target, utterance, names_for_id):
            vetoed.append(step)
            continue
        replacement = fallback_targets.get(step.tool, "")
        notes.append(
            f"「{step.target}」在用户原话里找不到依据，已退回确定性解析的说法"
            f"「{replacement or '（无指向，需要澄清）'}」"
        )
        vetoed.append(step.model_copy(update={"target": replacement}))
    return vetoed, notes


def clamp_plan(intent: Intent) -> Intent:
    """Fan-out is decided from the utterance, not from a model's ``all`` flag.

    A model that sets ``all=true`` for everything can light up the whole flat with one
    word; a model that forgets the flag when the user said "所有插座" would be asked to
    pick one. Both are corrected here from the user's wording.
    """
    if not intent.steps:
        return intent
    scoped = scope_requested(intent.utterance)
    steps: list[PlannedCall] = []
    for step in intent.steps:
        if step.all and not scoped:
            step = step.model_copy(update={"all": False})
            intent.notes.append("句子里没有范围词，已取消“全部设备”批量猜测，改为逐个指认")
        elif (
            not step.all
            and scoped
            and len(intent.steps) == 1
            and not any(word in (step.target or "") for word in SCOPE_WORDS)
        ):
            # 单步且用户明确说了“所有/都”，但解析器忘了置批量位：按用户原话补上。
            step = step.model_copy(update={"all": True})
            intent.notes.append("用户原话含范围词，已按批量处理")
        steps.append(step)
    intent.steps = steps
    return intent
