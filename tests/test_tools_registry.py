"""F-006: namespaced tool names, schema-constrained arguments, refusal of anything unknown."""

from __future__ import annotations

import pytest
from conftest import FixedParser, make_engine, step

from hvsl.errors import NamespaceViolationError, ToolValidationError, UnknownToolError
from hvsl.models import DecisionKind, PlannedCall
from hvsl.tools import NoArgs, ToolDef, ToolRegistry, build_default_registry


def test_registry_refuses_a_tool_without_the_namespace_prefix():
    registry = build_default_registry()
    with pytest.raises(NamespaceViolationError):
        registry.register(ToolDef("light.turn_on", "bare name", NoArgs, ("light", "turn_on")))


def test_registry_refuses_duplicate_tool_names():
    registry = ToolRegistry()
    registry.register(ToolDef("hvs.x.y", "one", NoArgs, ("x", "y")))
    with pytest.raises(ValueError):
        registry.register(ToolDef("hvs.x.y", "two", NoArgs, ("x", "y")))


def test_every_published_tool_carries_the_namespace():
    registry = build_default_registry()
    assert registry.names()
    assert all(name.startswith("hvs.") for name in registry.names())


def test_published_schema_pins_additional_properties_off():
    for schema in build_default_registry().json_schemas():
        assert schema["input_schema"]["additionalProperties"] is False
        assert "entity_ids" in schema["input_schema"]["required"]
        assert schema["name"].startswith("hvs.")
        assert schema["ha_service"]["domain"]


def test_arguments_are_validated_by_pydantic():
    registry = build_default_registry()
    assert registry.validate_args("hvs.light.turn_on", {"entity_ids": ["light.a"]}) == {"entity_ids": ["light.a"]}
    with pytest.raises(ToolValidationError):
        registry.validate_args("hvs.light.turn_on", {"entity_ids": ["light.a"], "colour": "red"})
    with pytest.raises(ToolValidationError):
        registry.validate_args("hvs.light.turn_on", {"entity_ids": []})
    with pytest.raises(ToolValidationError):
        registry.validate_args("hvs.light.turn_on", {})
    with pytest.raises(ToolValidationError):
        registry.validate_args("hvs.light.set_brightness", {"entity_ids": ["light.a"], "brightness_pct": 0})
    with pytest.raises(ToolValidationError):
        registry.validate_args("hvs.climate.set_temperature", {"entity_ids": ["climate.a"], "temperature_c": 400})
    with pytest.raises(UnknownToolError):
        registry.validate_args("hvs.light.laser_on", {"entity_ids": ["light.a"]})


def test_engine_rejects_an_unregistered_tool():
    engine = make_engine(parser=FixedParser([PlannedCall(tool="hvs.tv.demolish", target="客厅电视")]))
    decision = engine.handle("把电视拆了")
    assert decision.kind is DecisionKind.REJECTED
    assert "unknown tool" in decision.message or "hvs.tv.demolish" in decision.message
    assert engine.backend.dispatches == []
    engine.store.close()


def test_engine_rejects_a_bare_unnamespaced_tool_name():
    engine = make_engine(parser=FixedParser([PlannedCall(tool="light.turn_on", target="客厅大灯")]))
    assert engine.handle("开灯").kind is DecisionKind.REJECTED
    assert engine.backend.dispatches == []
    engine.store.close()


def test_engine_rejects_an_out_of_range_argument():
    engine = make_engine(parser=FixedParser([step("hvs.media_player.volume_set", "客厅电视", volume_level=140)]))
    decision = engine.handle("音量调到百分之一百四")
    assert decision.kind is DecisionKind.REJECTED
    assert "argument error" in decision.message
    assert engine.backend.dispatches == []
    engine.store.close()


def test_every_tool_maps_to_a_concrete_ha_service():
    registry = build_default_registry()
    for tool in registry.all():
        domain, service = tool.ha_service
        assert domain and service, tool.name
        resolved = registry.by_ha_service(domain, service)
        assert resolved is not None and resolved.ha_service == tool.ha_service
        assert tool.description, f"工具 {tool.name} 缺少描述，LLM 无法正确选择"


def test_engine_applies_dispatch_timeout_and_records_failure():
    from hvsl.backends.simulated import SimulatedBackend, load_catalog
    from hvsl.errors import DispatchTimeoutError
    from conftest import CATALOG

    class SlowBackend(SimulatedBackend):
        def __init__(self) -> None:
            super().__init__(load_catalog(CATALOG))

        def call_tool(self, tool, entity_ids, args):
            raise DispatchTimeoutError("HA 未在 8s 内响应")

    engine = make_engine(backend=SlowBackend(), parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]))
    decision = engine.handle("开灯")
    assert decision.kind is DecisionKind.PARTIAL
    assert "超时" in decision.message or "8s" in decision.message
    rows = engine.audit.query(dispatch_state="failed", limit=10)
    assert len(rows) == 1
    assert rows[0].error
    engine.store.close()
