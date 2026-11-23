"""Target resolution: unique match, whole-scope match, ambiguity, and no-match."""

from __future__ import annotations

import pytest
from conftest import FixedParser, make_engine, make_settings

from hvsl.errors import AmbiguousTargetError, NoTargetError
from hvsl.models import DecisionKind, PlannedCall
from hvsl.resolver import TargetResolver, rooms_in
from hvsl.tools import build_default_registry


@pytest.fixture()
def resolver(backend, registry) -> TargetResolver:
    return TargetResolver(backend)


def _resolve(resolver, registry, tool: str, target: str, *, all_scoped: bool = False) -> list[str]:
    return resolver.resolve(PlannedCall(tool=tool, target=target, all=all_scoped), registry.get(tool))


def test_display_name_resolves_to_one_entity(resolver, registry):
    assert _resolve(resolver, registry, "hvs.light.turn_on", "客厅大灯") == ["light.living_room_main"]


def test_alias_resolves_to_one_entity(resolver, registry):
    assert _resolve(resolver, registry, "hvs.switch.turn_on", "电暖器") == ["switch.heater_socket"]


def test_entity_id_passthrough(resolver, registry):
    assert _resolve(resolver, registry, "hvs.fan.turn_off", "fan.bedroom_fan") == ["fan.bedroom_fan"]


def test_bare_device_word_is_ambiguous(resolver, registry):
    with pytest.raises(AmbiguousTargetError) as exc:
        _resolve(resolver, registry, "hvs.light.turn_on", "灯")
    assert len(exc.value.candidates) == 4


def test_room_scoped_word_is_ambiguous_within_the_room(resolver, registry):
    with pytest.raises(AmbiguousTargetError) as exc:
        _resolve(resolver, registry, "hvs.light.turn_on", "客厅灯")
    assert set(exc.value.candidates) == {"客厅大灯", "客厅灯带"}


def test_all_flag_covers_every_entity_in_the_room(resolver, registry):
    ids = _resolve(resolver, registry, "hvs.light.turn_off", "客厅", all_scoped=True)
    assert sorted(ids) == ["light.living_room_main", "light.living_room_strip"]


def test_unknown_device_word_has_no_target(resolver, registry):
    with pytest.raises(NoTargetError):
        _resolve(resolver, registry, "hvs.light.turn_on", "按摩椅")


def test_domain_without_entities_is_refused(registry):
    from hvsl.backends.simulated import SimulatedBackend

    empty = SimulatedBackend([])
    resolver = TargetResolver(empty)
    with pytest.raises(NoTargetError):
        resolver.resolve(PlannedCall(tool="hvs.vacuum.start", target="扫地机器人"), registry.get("hvs.vacuum.start"))


def test_rooms_in_detects_chinese_room_words():
    assert rooms_in("把客厅和卧室的灯都关了") == ["客厅", "卧室"]
    assert rooms_in("开灯") == []


def test_engine_turns_ambiguity_into_a_clarification():
    engine = make_engine(parser=FixedParser([PlannedCall(tool="hvs.light.turn_on", target="灯")]))
    decision = engine.handle("把灯打开")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED
    assert len(decision.candidates) == 4
    assert decision.dispatch_count == 0
    assert engine.backend.dispatches == []
    engine.store.close()


def test_engine_turns_no_match_into_a_rejection():
    engine = make_engine(parser=FixedParser([PlannedCall(tool="hvs.light.turn_on", target="按摩椅")]))
    decision = engine.handle("打开按摩椅")
    assert decision.kind is DecisionKind.REJECTED
    assert "不在受控清单" in decision.message
    assert engine.backend.dispatches == []
    engine.store.close()


def test_ambiguous_session_confirm_asks_which_one():
    engine = make_engine(
        settings=make_settings(),
        parser=FixedParser([PlannedCall(tool="hvs.lock.unlock", target="入户门锁")]),
    )
    engine.handle("解锁大门")
    engine.handle("解锁大门")
    # 两条待确认指令，裸 confirm 必须让人指认，而不是随便挑一条执行
    from hvsl.models import DecisionKind as K

    assert len(engine.pending()) == 2
    decision = engine.confirm()
    assert decision.kind is K.CLARIFICATION_REQUIRED
    assert engine.backend.dispatches == []
    engine.store.close()
