"""护栏与解析器无关：模型不能自己宣布“这没有歧义”“这就是全部设备”。"""

from __future__ import annotations

from conftest import CATALOG, FixedParser, make_engine, make_settings, step

from hvsl import guards
from hvsl.backends.simulated import SimulatedBackend, load_domain_words
from hvsl.models import DecisionKind, Intent, PlannedCall
from hvsl.resolver import TargetResolver
from hvsl.tools import build_default_registry


class EchoParser:
    """模拟 LLM 提供的“已经很确定”的计划，用来验证引擎层的钳制。"""

    def __init__(self, steps: list[PlannedCall], provider: str = "llm") -> None:
        self.steps = steps
        self.provider = provider

    def parse(self, utterance: str) -> Intent:
        return Intent(utterance=utterance, steps=[s.model_copy(deep=True) for s in self.steps], provider=self.provider)


def test_schedule_words_are_refused_regardless_of_parser():
    engine = make_engine(
        parser=EchoParser([PlannedCall(tool="hvs.light.turn_on", target="客厅大灯")])
    )
    decision = engine.handle("明天早上七点打开客厅大灯")
    assert decision.kind is DecisionKind.REJECTED
    assert "unsupported_schedule" in decision.message
    assert engine.backend.dispatches == []
    engine.store.close()


def test_model_claimed_fan_out_without_a_scope_word_is_clamped():
    """模型对所有灯都下发？句子没说“都/全部/所有”，那就改成问用户。"""
    engine = make_engine(
        parser=EchoParser([PlannedCall(tool="hvs.light.turn_on", target="灯", all=True)])
    )
    decision = engine.handle("把灯打开")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED, decision.message
    assert len(decision.candidates) == 4
    assert engine.backend.dispatches == []
    engine.store.close()


def test_real_scope_word_still_fans_out():
    engine = make_engine(
        parser=EchoParser([PlannedCall(tool="hvs.light.turn_on", target="家里的灯", all=True)])
    )
    decision = engine.handle("把家里的灯全部打开")
    assert decision.kind is DecisionKind.EXECUTED
    assert len(decision.steps[0].targets) == 4
    engine.store.close()


def test_fan_out_can_also_come_from_the_phrase():
    """解析器忘了置 all，但目标短语里写着“所有插座”：仍然按批量处理。"""
    from hvsl.resolver import TargetResolver

    backend = SimulatedBackend.from_catalog(CATALOG)
    resolver = TargetResolver(backend)
    ids = resolver.resolve(PlannedCall(tool="hvs.switch.turn_off", target="所有插座"), build_default_registry().get("hvs.switch.turn_off"))
    assert len(ids) == 5
    single = resolver.resolve(PlannedCall(tool="hvs.switch.turn_off", target="加湿器"), build_default_registry().get("hvs.switch.turn_off"))
    assert single == ["switch.humidifier"]


def test_clamp_plan_reports_what_it_changed():
    intent = Intent(
        utterance="把灯打开",
        steps=[PlannedCall(tool="hvs.light.turn_on", target="灯", all=True)],
        provider="llm",
    )
    clamped = guards.clamp_plan(intent)
    assert clamped.steps[0].all is False
    assert any("取消" in note for note in clamped.notes)

    scoped = guards.clamp_plan(
        Intent(utterance="把灯都打开", steps=[PlannedCall(tool="hvs.light.turn_on", target="灯", all=True)])
    )
    assert scoped.steps[0].all is True


def test_unsupported_and_scope_vocabulary():
    assert guards.unsupported_reason("半小时后关空调") == "半小时后"
    assert guards.unsupported_reason("关掉空调") is None
    assert guards.scope_requested("所有灯都关掉") is True
    assert guards.scope_requested("关掉客厅大灯") is False


def test_window_is_not_a_curtain():
    """「把窗户打开」不能顺手拉开窗帘。"""
    backend = SimulatedBackend.from_catalog(CATALOG)
    resolver = TargetResolver(backend)
    import pytest

    from hvsl.errors import NoTargetError

    with pytest.raises(NoTargetError, match="不在受控清单"):
        resolver.resolve(
            PlannedCall(tool="hvs.cover.open", target="窗户"), build_default_registry().get("hvs.cover.open")
        )
    assert resolver.resolve(
        PlannedCall(tool="hvs.cover.open", target="窗帘"), build_default_registry().get("hvs.cover.open")
    ) == ["cover.living_room_curtain"]


def test_domain_words_come_from_the_reviewed_catalog():
    words = load_domain_words(CATALOG)
    assert "灯" in words["light"] and "插座" in words["switch"]
    assert "窗户" not in words.get("cover", [])
    backend = SimulatedBackend.from_catalog(CATALOG)
    assert backend.domain_words == words


def _shadow_engine(target: PlannedCall, utterance: str, provider: str = "llm"):
    from hvsl.backends.simulated import SimulatedBackend
    from hvsl.engine import DecisionEngine
    from hvsl.risk import RiskPolicy
    from hvsl.store import Store
    from hvsl.tools import build_default_registry
    from pipeline.nlu import RuleParser

    registry = build_default_registry()
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = DecisionEngine(
        backend,
        EchoParser([target], provider=provider),
        settings=make_settings(),
        registry=registry,
        policy=RiskPolicy.load(make_settings().policy_path),
        store=Store(":memory:"),
        cross_check=RuleParser(registry),
    )
    return engine, backend


def test_entity_id_is_accepted_when_the_user_named_that_device():
    engine, _backend = _shadow_engine(
        PlannedCall(tool="hvs.fan.turn_on", target="fan.bedroom_fan"), "把卧室风扇打开"
    )
    decision = engine.handle("把卧室风扇打开")
    assert decision.kind is DecisionKind.EXECUTED, decision.message
    assert decision.steps[0].targets == ["fan.bedroom_fan"]
    engine.store.close()


def test_entity_id_cannot_bypass_the_utterance_check():
    """模型直接给 light.living_room_main，但用户只说了“灯”：必须回头问，不能自己挑一盏。"""
    engine, backend = _shadow_engine(PlannedCall(tool="hvs.light.turn_on", target="light.living_room_main"), "把灯打开")
    decision = engine.handle("把灯打开")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED, decision.message
    assert len(decision.candidates) == 4
    assert backend.dispatches == []
    rows = engine.audit.query(stage="nlu", limit=5)
    assert any("找不到依据" in note for row in rows for note in row.detail.get("notes", []))
    engine.store.close()


def test_invented_device_name_is_vetoed_by_the_shadow_parser():
    """模型把「打开客厅的灯」直接答成“客厅大灯”？纠偏回用户原话的说法，改为问用户。"""
    engine, backend = _shadow_engine(PlannedCall(tool="hvs.light.turn_on", target="客厅大灯"), "打开客厅的灯")
    decision = engine.handle("打开客厅的灯")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED, decision.message
    assert set(decision.candidates) == {"客厅大灯", "客厅灯带"}
    assert backend.dispatches == []
    assert decision.degraded is True, "纠偏过的事实必须写进留痕"
    engine.store.close()


def test_wrong_tool_choice_fails_safe_instead_of_guessing():
    """模型给「把大门打开」选了开窗帘的工具：拒绝，不去执行一个用户没提的设备。"""
    engine, backend = _shadow_engine(PlannedCall(tool="hvs.cover.open", target="入户门锁"), "把大门打开")
    decision = engine.handle("把大门打开")
    assert decision.kind is DecisionKind.REJECTED, decision.message
    assert backend.dispatches == []
    engine.store.close()


def test_rule_parser_needs_no_shadow_and_still_resolves_normal_phrasing():
    """纠偏器只在 llm 路径介入：规则路径自己就是确定性来源。"""
    engine = make_engine(parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]))
    assert engine.cross_check is None
    assert engine.handle("打开客厅大灯").kind is DecisionKind.EXECUTED
    engine.store.close()


def test_shadow_check_runs_even_when_the_provider_label_is_rule():
    """F3：provider 标签不再决定是否纠偏。

    LLM 失败降级后 provider 会变成 ``rule``；HA 的 LLM 路径如果按标签跳过，
    模型/降级方案里凭空出现的设备名就会被直接执行。这里证明只要装配了
    ``cross_check``，无依据目标仍会被否决成澄清。
    """
    engine, backend = _shadow_engine(
        PlannedCall(tool="hvs.light.turn_on", target="客厅大灯"), "打开客厅的灯", provider="rule"
    )
    decision = engine.handle("打开客厅的灯")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED, decision.message
    assert decision.nlu_provider == "rule"
    assert backend.dispatches == []
    assert decision.degraded is True, "纠偏过的事实必须写进留痕"
    engine.store.close()


def test_grounding_check_rules():
    assert guards.grounded_in("客厅大灯", "把客厅大灯打开") is True
    assert guards.grounded_in("客厅大灯", "打开客厅的灯") is False
    assert guards.grounded_in("", "把灯打开") is True
    assert guards.grounded_in("电暖器", "给电暖器上电") is True
    names = {"light.living_room_main": ["客厅大灯", "客厅主灯", "大灯", "主灯"]}
    assert guards.grounded_in("light.living_room_main", "关掉客厅主灯", names) is True
    assert guards.grounded_in("light.living_room_main", "把灯打开", names) is False
    # 清单里查无此 id：交给 resolver 报“无匹配设备”，不在这里放行成执行
    assert guards.grounded_in("light.attic", "把灯打开", names) is True
    engine = make_engine(parser=FixedParser([PlannedCall(tool="hvs.light.turn_off", target="灯")]))
    decision = engine.handle("关灯")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED
    assert engine.backend.dispatches == []
    engine.store.close()
