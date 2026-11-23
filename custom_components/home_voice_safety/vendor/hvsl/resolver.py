"""Target resolution: Chinese phrase -> concrete entity ids, or a clarify/no-target outcome.

Vocabulary is derived from the reviewed device catalog (display names + aliases +
``room:*`` tags), so adding a device only means editing ``devices/devices.json``.
"""

from __future__ import annotations

import re

from .backends.base import DeviceBackend
from .errors import AmbiguousTargetError, NoTargetError
from .models import DeviceEntity, PlannedCall
from .tools import ToolDef

_ROOM_WORDS = ("客厅", "卧室", "主卧", "次卧", "书房", "厨房", "玄关", "阳台", "卫生间", "餐厅", "儿童房")
_ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def rooms_in(phrase: str) -> list[str]:
    return [room for room in _ROOM_WORDS if room in phrase]


class TargetResolver:
    def __init__(self, backend: DeviceBackend, domain_words: dict[str, list[str]] | None = None) -> None:
        self.backend = backend
        self.domain_words = domain_words if domain_words is not None else getattr(backend, "domain_words", {})

    def resolve(self, planned: PlannedCall, tool: ToolDef, grounding_text: str = "") -> list[str]:
        """Map one planned call onto entity ids.

        ``grounding_text`` is the user's own words: when the parser left the target
        empty, the class word still has to come from the utterance, otherwise
        "把窗户打开" would be free to move the curtain.
        """
        phrase = (planned.target or "").strip()
        probe = phrase or (grounding_text or "").strip()
        domain = tool.ha_service[0]
        pool = [e for e in self.backend.entities() if e.domain == domain]
        if not pool:
            raise NoTargetError(f"没有可用的 {domain} 实体（受控清单里没有该类型设备）")

        exact = self._by_entity_id(phrase, pool)
        if exact is not None:
            return [exact.entity_id]

        named = self._by_name(phrase, pool)
        rooms = rooms_in(phrase)

        if named:
            narrowed = [e for e in named if not rooms or self._has_room(e, rooms)]
            if not narrowed and rooms:
                # 「打开书房空调」不能悄悄执行客厅空调：房间说死了却没对上，就回头问用户。
                raise AmbiguousTargetError(
                    f"{'、'.join(rooms)}里没有匹配「{phrase}」的设备，是否指："
                    + "、".join(e.display_name for e in named),
                    [e.display_name for e in named],
                )
            candidates = narrowed or named
        elif rooms:
            candidates = [e for e in pool if self._has_room(e, rooms)]
        elif probe and not self._hints_any_name(probe, pool, domain):
            # 「把窗户打开」不能顺手拉开窗帘：目标或用户原话里都要能 hint 到本域设备名。
            raise NoTargetError(f"「{phrase or probe}」不在受控清单的设备词表里，未执行任何动作")
        else:
            candidates = list(pool)

        if not candidates:
            raise NoTargetError(f"找不到与「{phrase or '该设备'}」匹配的设备")
        if planned.fan_out():
            return [e.entity_id for e in candidates]
        if len(candidates) == 1:
            return [candidates[0].entity_id]
        raise AmbiguousTargetError(
            f"「{phrase}」匹配到 {len(candidates)} 个设备，请说明是哪一个",
            [e.display_name for e in candidates],
        )

    @staticmethod
    def _has_room(entity: DeviceEntity, rooms: list[str]) -> bool:
        entity_rooms = {t.split(":", 1)[1] for t in entity.tags if t.startswith("room:")}
        return bool(set(rooms) & entity_rooms) or any(room in entity.display_name for room in rooms)

    @staticmethod
    def _by_entity_id(phrase: str, pool: list[DeviceEntity]) -> DeviceEntity | None:
        if _ENTITY_ID_RE.match(phrase):
            for entity in pool:
                if entity.entity_id == phrase:
                    return entity
        return None

    def _hints_any_name(self, phrase: str, pool: list[DeviceEntity], domain: str) -> bool:
        """Can the phrase be tied to this domain, in either direction?

        The generic vocabulary lives in ``devices/devices.json`` -> ``domain_words``,
        because it is a reviewed safety decision: "灯" may legitimately fan out over
        four lights (clarification), while "窗户" must be refused instead of silently
        moving a curtain.
        """
        vocabulary: list[str] = list(self.domain_words.get(domain, []))
        for entity in pool:
            vocabulary.extend([entity.display_name, *entity.aliases])
        return any(word and (word in phrase or phrase in word) for word in vocabulary)

    @staticmethod
    def _by_name(phrase: str, pool: list[DeviceEntity]) -> list[DeviceEntity]:
        if not phrase:
            return []
        hits: list[tuple[int, DeviceEntity]] = []
        for entity in pool:
            words = [entity.display_name, *entity.aliases]
            matched = [w for w in words if w and w in phrase]
            if matched:
                hits.append((max(len(w) for w in matched), entity))
        if not hits:
            return []
        best = max(score for score, _ in hits)
        return [entity for score, entity in hits if score == best]
