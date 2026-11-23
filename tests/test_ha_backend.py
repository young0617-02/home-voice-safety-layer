"""F-002/F-003: contract tests for the Home Assistant REST adapter.

These run against a local stub that speaks the HA REST shapes
(``GET /api/states`` and ``POST /api/services/<domain>/<service>``). They prove
the wire format, the error mapping and that the safety gate sits in front of real
service calls. They do **not** replace a live-HA smoke run, which is still on the
to-do list in 交付总结.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import CATALOG, FixedParser, make_engine, step

from hvsl.backends.home_assistant import HomeAssistantBackend, _to_ha_payload
from hvsl.errors import BackendUnavailableError, DispatchTimeoutError
from hvsl.models import DecisionKind, Provenance
from hvsl.risk import RiskPolicy
from hvsl.tools import build_default_registry

POLICY = "hvsl/policy/risk_policy.json"

STATES = [
    {"entity_id": "light.living_room_main", "state": "off", "attributes": {"friendly_name": "客厅大灯"}},
    {"entity_id": "switch.gas_valve", "state": "off", "attributes": {"friendly_name": "燃气(本机自报安全)", "tags": ["safe"]}},
    {"entity_id": "switch.main_breaker", "state": "on", "attributes": {"friendly_name": "总闸"}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "入户门锁"}},
    {"entity_id": "switch.boom", "state": "off", "attributes": {"friendly_name": "故障插座"}},
]


class StubHandler(BaseHTTPRequestHandler):
    requests: list[dict] = []

    def log_message(self, *args):  # silence the stub
        return None

    def _send(self, code: int, body) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):  # noqa: N802 - http.server API
        if self.path != "/api/states":
            self._send(404, {"message": "not found"})
            return
        if self.headers.get("Authorization") != "Bearer good-token":
            self._send(401, {"message": "unauthorized"})
            return
        self._send(200, STATES)

    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        StubHandler.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
        if self.headers.get("Authorization") != "Bearer good-token":
            self._send(401, {"message": "unauthorized"})
            return
        if body.get("entity_id") == "cover.slow":
            time.sleep(0.6)
            self._send(200, [])
            return
        if body.get("entity_id") == "switch.boom":
            self._send(500, {"message": "service failed"})
            return
        self._send(200, [{"entity_id": body.get("entity_id"), "state": "on"}])


@pytest.fixture(scope="module")
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture()
def ha_backend(stub) -> HomeAssistantBackend:
    StubHandler.requests.clear()
    return HomeAssistantBackend(
        stub,
        "good-token",
        timeout=4.0,
        real_entities="light.living_room_main",
        catalog_path=CATALOG,
    )


def test_states_are_merged_with_the_reviewed_catalog(ha_backend):
    entities = {e.entity_id: e for e in ha_backend.entities()}
    assert set(entities) == {"light.living_room_main", "switch.gas_valve", "switch.main_breaker", "lock.front_door", "switch.boom"}
    assert entities["light.living_room_main"].provenance is Provenance.REAL
    assert entities["switch.gas_valve"].provenance is Provenance.SIMULATED
    assert entities["switch.gas_valve"].display_name == "厨房燃气电磁阀"


def test_a_device_cannot_talk_its_way_out_of_the_danger_list(ha_backend):
    """The stub reports switch.gas_valve as "safe"; the catalog tag must still win."""
    entity = {e.entity_id: e for e in ha_backend.entities()}["switch.gas_valve"]
    assert "danger:gas" in entity.tags
    policy = RiskPolicy.load(POLICY)
    assert policy.classify("hvs.switch.turn_on", [entity])[0].value == "deny"
    assert policy.classify("hvs.switch.turn_off", [entity])[0].value == "confirm"


def test_locks_get_their_security_tag_even_without_a_catalog_entry(ha_backend):
    entity = {e.entity_id: e for e in ha_backend.entities()}["lock.front_door"]
    assert "danger:security" in entity.tags


def test_states_request_is_authorized(ha_backend):
    assert ha_backend.entities()
    assert StubHandler.requests == []  # GET is not recorded by the POST-only recorder
    bad = HomeAssistantBackend(ha_backend.base_url, "wrong-token", catalog_path=CATALOG)
    with pytest.raises(BackendUnavailableError):
        bad.entities()


def test_service_call_payload_uses_ha_field_names(ha_backend):
    registry = build_default_registry()
    result = ha_backend.call_tool(
        registry.get("hvs.light.turn_on"), ["light.living_room_main"], {"entity_ids": ["light.living_room_main"], "brightness_pct": 50}
    )
    assert result["ok"] is True
    recorded = StubHandler.requests[-1]
    assert recorded["path"] == "/api/services/light/turn_on"
    assert recorded["body"] == {"brightness": 127, "entity_id": "light.living_room_main"}
    assert recorded["auth"] == "Bearer good-token"


def test_multi_entity_call_posts_a_list(ha_backend):
    registry = build_default_registry()
    ha_backend.call_tool(
        registry.get("hvs.light.turn_off"),
        ["light.living_room_main", "light.study_desk"],
        {"entity_ids": ["light.living_room_main", "light.study_desk"]},
    )
    body = StubHandler.requests[-1]["body"]
    assert body["entity_id"] == ["light.living_room_main", "light.study_desk"]
    assert body == {"entity_id": ["light.living_room_main", "light.study_desk"]}


def test_argument_translation_to_ha_schema():
    assert _to_ha_payload({"entity_ids": ["a"], "temperature_c": 26}) == {"temperature": 26.0}
    assert _to_ha_payload({"entity_ids": ["a"], "volume_level": 40}) == {"volume_level": 0.4}
    assert _to_ha_payload({"entity_ids": ["a"], "mode": "cool"}) == {"hvac_mode": "cool"}
    assert _to_ha_payload({"entity_ids": ["a"], "percentage": 30, "code": None}) == {"percentage": 30}


def test_service_error_maps_to_backend_unavailable(ha_backend):
    registry = build_default_registry()
    with pytest.raises(BackendUnavailableError):
        ha_backend.call_tool(registry.get("hvs.switch.turn_on"), ["switch.boom"], {"entity_ids": ["switch.boom"]})


def test_timeout_maps_to_dispatch_timeout(stub):
    backend = HomeAssistantBackend(stub, "good-token", timeout=0.2, catalog_path=CATALOG)
    registry = build_default_registry()
    with pytest.raises(DispatchTimeoutError):
        backend.call_tool(registry.get("hvs.cover.open"), ["cover.slow"], {"entity_ids": ["cover.slow"]})


def test_unreachable_host_is_reported_as_a_hvsl_error():
    """A dropped port surfaces as a connect timeout; either way the caller gets a
    typed error, never a raw socket exception."""
    from hvsl.errors import HvslError

    backend = HomeAssistantBackend("http://127.0.0.1:9", "token", timeout=0.5, catalog_path=CATALOG)
    with pytest.raises(HvslError):
        backend.entities()


def test_missing_base_url_is_a_configuration_error():
    with pytest.raises(BackendUnavailableError):
        HomeAssistantBackend("", "token")


def test_dangerous_action_over_the_ha_wire_still_needs_confirmation(ha_backend):
    """A1 re-proved on the HA transport: nothing is POSTed until the user confirms."""
    StubHandler.requests.clear()
    policy = RiskPolicy.load(POLICY)
    engine = make_engine(
        backend=ha_backend,
        parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]),
        policy=policy,
        registry=build_default_registry(),
    )
    parked = engine.handle("解锁大门")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    assert StubHandler.requests == [], "未确认就向 HA 下发了服务调用"

    confirmed = engine.confirm(parked.confirmation_token)
    posts = [r["path"] for r in StubHandler.requests]
    assert confirmed.kind is DecisionKind.EXECUTED, confirmed.message
    assert posts == ["/api/services/lock/unlock"]
    engine.store.close()


def test_ha_backend_survives_a_missing_catalog_entity(ha_backend):
    """switch.boom is not in devices.json: it still lists, without danger tags,
    and is marked ``reviewed=False`` so the risk policy can floor it (F1)."""
    entity = {e.entity_id: e for e in ha_backend.entities()}["switch.boom"]
    assert entity.tags == []
    assert entity.display_name == "故障插座"
    assert entity.reviewed is False
    reviewed = {e.entity_id: e.reviewed for e in ha_backend.entities()}
    assert reviewed["switch.gas_valve"] is True, "catalog 内实体必须保持 reviewed=True"
