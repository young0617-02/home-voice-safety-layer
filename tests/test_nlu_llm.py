"""F-006 LLM provider: schema-constrained output, hard timeout, degradation to the rule parser."""

from __future__ import annotations

import json
import os

import pytest
from conftest import make_settings

from hvsl.tools import build_default_registry
from pipeline.nlu import LlmParser, RuleParser

RUN_LLM = os.environ.get("HVSL_RUN_LLM_TESTS") == "1"


def make_llm_parser(base_url="http://127.0.0.1:9", **overrides):
    settings = make_settings(nlu_provider="llm", ollama_base_url=base_url, nlu_timeout_seconds=1.0, **overrides)
    registry = build_default_registry()
    catalog = [{"entity_id": "light.living_room_main", "display_name": "客厅大灯"}]
    return LlmParser(settings, registry=registry, fallback=RuleParser(registry), catalog=catalog)


def test_unreachable_endpoint_degrades_to_the_rule_parser_without_raising():
    parser = make_llm_parser()
    intent = parser.parse("打开客厅大灯")
    assert intent.steps, "降级后必须仍有解析结果"
    assert intent.steps[0].tool == "hvs.light.turn_on"
    assert intent.degraded is True
    assert "rule" in intent.provider
    assert any("降级" in note for note in intent.notes)


def test_cloud_endpoint_is_refused_unless_explicitly_allowed():
    with pytest.raises(ValueError, match="loopback"):
        make_llm_parser(base_url="https://api.example.com")
    parser = make_llm_parser(base_url="https://api.example.com", allow_cloud_llm=True)
    assert parser.system_prompt.startswith("你是智能家居指令解析器")


def test_system_prompt_publishes_only_namespaced_tools():
    parser = make_llm_parser()
    assert '"hvs.light.turn_on"' in parser.system_prompt
    assert '"light.turn_on"' not in parser.system_prompt
    assert "客厅大灯" in parser.system_prompt


def test_llm_output_is_validated_and_filtered():
    parser = make_llm_parser()
    payload = {
        "steps": [
            {"tool": "hvs.light.turn_on", "target": "客厅大灯", "all": False, "args": {}},
            {"tool": "light.turn_on", "target": "客厅大灯"},  # 没有命名空间，必须丢弃
            {"tool": "hvs.light.burn_house", "target": "客厅"},  # 不存在的工具，必须丢弃
            {"tool": "hvs.climate.set_temperature", "target": "空调", "args": {"temperature_c": 400}},
            {"tool": "hvs.switch.turn_on", "entity_ids": ["switch.heater_socket"]},  # 实体 id 形式
        ]
    }
    steps = parser._normalise(payload)
    # 解析层只保证「工具名合法 + 结构合法」；越界参数交给引擎的 Pydantic 校验（见 test_tools_registry）
    assert [s.tool for s in steps] == ["hvs.light.turn_on", "hvs.climate.set_temperature", "hvs.switch.turn_on"]
    assert steps[2].target == "switch.heater_socket"


def test_out_of_range_llm_argument_is_refused_by_the_engine():
    from conftest import FixedParser, make_engine

    from hvsl.models import DecisionKind, PlannedCall

    engine = make_engine(
        parser=FixedParser([PlannedCall(tool="hvs.climate.set_temperature", target="客厅空调", args={"temperature_c": 400})])
    )
    decision = engine.handle("空调调到四百度")
    assert decision.kind is DecisionKind.REJECTED
    assert engine.backend.dispatches == []
    engine.store.close()


def test_garbage_llm_output_degrades():
    parser = make_llm_parser()
    parser._chat = lambda text: {"note": "我不会输出 JSON"}
    intent = parser.parse("打开客厅大灯")
    assert intent.degraded is True
    assert intent.steps[0].tool == "hvs.light.turn_on"


def test_valid_llm_output_is_used_directly():
    parser = make_llm_parser()
    parser._chat = lambda text: {"steps": [{"tool": "hvs.cover.close", "target": "客厅窗帘", "all": False, "args": {}}]}
    intent = parser.parse("拉上窗帘")
    assert intent.provider == "llm"
    assert intent.degraded is False
    assert intent.steps[0].tool == "hvs.cover.close"


def test_llm_plan_is_cross_checked_against_the_rule_parser():
    """F3：HA 的 LLM 路径装配 cross_check 后，模型凭空挑设备必须被否决成澄清。"""
    from conftest import make_engine

    from hvsl.models import DecisionKind

    parser = make_llm_parser()
    parser._chat = lambda text: {
        "steps": [{"tool": "hvs.light.turn_on", "target": "客厅大灯", "all": False, "args": {}}]
    }
    engine = make_engine(parser=parser)
    engine.cross_check = RuleParser(build_default_registry())

    decision = engine.handle("打开客厅的灯")
    assert decision.kind is DecisionKind.CLARIFICATION_REQUIRED, decision.message
    assert decision.nlu_provider == "llm"
    assert decision.degraded is True, "被影子解析纠偏后必须标记 degraded"
    assert engine.backend.dispatches == []
    engine.store.close()


@pytest.mark.skipif(not RUN_LLM, reason="需要本机 Ollama，设 HVSL_RUN_LLM_TESTS=1 才跑")
def test_live_ollama_parse_roundtrip():
    settings = make_settings(nlu_provider="llm", ollama_base_url=os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434"))
    registry = build_default_registry()
    parser = LlmParser(settings, registry=registry, fallback=RuleParser(registry))
    intent = parser.parse("把客厅大灯打开")
    assert intent.steps
    assert intent.steps[0].tool.startswith("hvs.")
    assert json.dumps(intent.model_dump(), ensure_ascii=False)
