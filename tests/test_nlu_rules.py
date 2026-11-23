"""F-006 rule parser: deterministic Chinese utterance -> namespaced tool plan."""

from __future__ import annotations

import pytest
from hvsl.models import PlannedCall

from pipeline.nlu import RuleParser, chinese_number

CATALOG = "devices/devices.json"


@pytest.fixture(scope="module")
def parser():
    from hvsl.tools import build_default_registry

    return RuleParser(build_default_registry())


def steps_of(parser, text: str) -> list[PlannedCall]:
    return parser.parse(text).steps


@pytest.mark.parametrize(
    "utterance,tool,target,args",
    [
        ("打开客厅大灯", "hvs.light.turn_on", "客厅大灯", {}),
        ("关闭书房台灯", "hvs.light.turn_off", "书房台灯", {}),
        ("把客厅大灯亮度调到60%", "hvs.light.set_brightness", "客厅大灯", {"brightness_pct": 60}),
        ("打开电暖器", "hvs.switch.turn_on", "电暖器", {}),
        ("关闭总电源", "hvs.switch.turn_off", "总电源", {}),
        ("打开燃气阀", "hvs.switch.turn_on", "燃气阀", {}),
        ("关掉厨房的燃气", "hvs.switch.turn_off", "厨房燃气", {}),
        ("解锁入户门锁", "hvs.lock.unlock", "入户门锁", {}),
        ("把门锁锁上", "hvs.lock.lock", "门锁", {}),
        ("撤防", "hvs.alarm.disarm", "", {}),
        ("外出布防", "hvs.alarm.arm_away", "", {}),
        ("空调调到二十六度", "hvs.climate.set_temperature", "空调", {"temperature_c": 26.0}),
        ("空调设为制冷", "hvs.climate.set_hvac_mode", "空调", {"mode": "cool"}),
        ("打开卧室空调", "hvs.climate.turn_on", "卧室空调", {}),
        ("风扇调到三档", "hvs.fan.set_percentage", "风扇", {"percentage": 75}),
        ("电视音量调到40", "hvs.media_player.volume_set", "电视", {"volume_level": 40}),
        ("开始扫地", "hvs.vacuum.start", "扫地", {}),
        ("让扫地机回充", "hvs.vacuum.return_home", "扫地机", {}),
        ("停止扫地", "hvs.vacuum.stop", "扫地", {}),
        ("打开窗帘", "hvs.cover.open", "窗帘", {}),
        ("拉上客厅窗帘", "hvs.cover.close", "客厅窗帘", {}),
        ("打开加湿器", "hvs.switch.turn_on", "加湿器", {}),
        ("把客厅窗帘开到50%", "hvs.cover.set_position", "客厅窗帘", {"percentage": 50}),
    ],
)
def test_single_step_parses_to_the_expected_tool(parser, utterance, tool, target, args):
    steps = steps_of(parser, utterance)
    assert len(steps) == 1, f"{utterance} 解析出 {len(steps)} 步：{steps}"
    step = steps[0]
    assert step.tool == tool
    assert step.args == args
    assert target in step.target


@pytest.mark.parametrize(
    "utterance,count",
    [("把客厅的灯都关掉", 2), ("帮我把家里的灯全部打开", 4), ("打开所有插座", 5), ("卧室的灯全关掉", 1)],
)
def test_scope_words_do_not_collapse_targets(parser, utterance, count):
    """The parser hands a scope phrase to the resolver; fan-out is the resolver's job."""
    from hvsl.backends.simulated import SimulatedBackend
    from hvsl.resolver import TargetResolver

    backend = SimulatedBackend.from_catalog(CATALOG)
    resolver = TargetResolver(backend)
    steps = steps_of(parser, utterance)
    assert len(steps) == 1
    resolved = resolver.resolve(steps[0], parser.registry.get(steps[0].tool))
    assert len(resolved) == count, f"{utterance} -> {resolved}"


def test_multi_step_plan_keeps_its_order(parser):
    steps = steps_of(parser, "关掉客厅大灯，然后解锁入户门锁")
    assert [s.tool for s in steps] == ["hvs.light.turn_off", "hvs.lock.unlock"]


def test_multi_step_plan_with_three_actions(parser):
    steps = steps_of(parser, "打开空调调到二十六度、拉上窗帘、再把扫地机启动")
    tools = [s.tool for s in steps]
    assert "hvs.climate.set_temperature" in tools
    assert "hvs.cover.close" in tools
    assert "hvs.vacuum.start" in tools


def test_unrecognised_request_produces_no_steps(parser):
    intent = parser.parse("今天天气怎么样")
    assert intent.steps == []
    assert intent.name == "unparsed"


def test_provider_and_degradation_are_reported(parser):
    intent = parser.parse("打开客厅大灯")
    assert intent.provider == "rule"
    assert intent.degraded is False


@pytest.mark.parametrize(
    "raw,expected",
    [("26", 26), ("二十六", 26), ("十", 10), ("三", 3), ("四十", 40), ("一百", None), ("abc", None)],
)
def test_chinese_numerals(raw, expected):
    assert chinese_number(raw) == expected
