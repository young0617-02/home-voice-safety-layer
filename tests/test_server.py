"""HTTP contract (F-010 local-only interface): A1/A2 red lines must hold over the API too.

The contract lives in :func:`hvsl.server.handle_request`, which is framework
agnostic; the FastAPI adapter is exercised only when TestClient actually works in
the environment (some installs have an anyio/typing_extensions mismatch, which is
not our code's problem and must not look like a functional failure).
"""

from __future__ import annotations

import pytest
from conftest import CATALOG, make_settings

from hvsl import server as server_module
from hvsl.app import build_engine
from hvsl.config import Settings
from hvsl.store import Store

POLICY = "hvsl/policy/risk_policy.json"


@pytest.fixture()
def api():
    settings = Settings(
        db_path=":memory:",
        policy_path=POLICY,
        device_catalog=CATALOG,
        confirm_ttl_seconds=60.0,
        dedup_window_seconds=30.0,
    )
    engine = build_engine(settings, store=Store(":memory:"))
    calls = []

    def request(method: str, path: str, body: dict | None = None):
        status, content = server_module.handle_request(engine, method, path, body or {})
        calls.append((method, path, status))
        return status, content

    yield request, engine
    engine.store.close()


def test_health_and_devices_endpoints(api):
    request, engine = api
    status, body = request("GET", "/v1/health")
    assert status == 200
    assert body["ok"] is True and body["backend"] == "simulated" and body["entities"] >= 10

    status, devices = request("GET", "/v1/devices")
    assert status == 200
    assert {device["provenance"] for device in devices} <= {"real", "simulated"}
    assert all(device["entity_id"] for device in devices)


def test_unknown_route_is_a_404_with_the_route_list(api):
    request, _ = api
    status, body = request("GET", "/v1/nope")
    assert status == 404
    assert "/v1/utterance" in body["available"]


def test_dangerous_action_stays_parked_over_http(api):
    request, engine = api
    _, parked = request("POST", "/v1/utterance", {"text": "解锁入户门锁", "session_id": "http"})
    assert parked["kind"] == "awaiting_confirmation"
    assert parked["dispatch_count"] == 0
    assert engine.backend.dispatches == []

    _, listed = request("GET", "/v1/pending", {"session_id": "http"})
    assert len(listed) == 1
    assert listed[0]["risk_class"] == "door_unlock"

    _, audit_rows = request("GET", "/v1/audit", {"trace_id": parked["trace_id"]})
    assert any(row["stage"] == "confirm" and row["confirmation_state"] == "pending" for row in audit_rows)

    _, done = request("POST", "/v1/confirm", {"token": parked["confirmation_token"], "session_id": "http"})
    assert done["kind"] == "executed" and done["dispatch_count"] == 1

    _, again = request("POST", "/v1/confirm", {"token": parked["confirmation_token"], "session_id": "http"})
    assert again["kind"] == "rejected" and again["dispatch_count"] == 0
    assert len(engine.backend.dispatches) == 1, "同一个确认令牌被用了两次"


def test_gas_valve_open_is_refused_and_replay_is_deduplicated(api):
    request, _ = api
    _, denied = request("POST", "/v1/utterance", {"text": "打开燃气阀"})
    assert denied["kind"] == "rejected" and denied["dispatch_count"] == 0

    _, first = request("POST", "/v1/utterance", {"text": "打开书房台灯", "session_id": "dup"})
    _, second = request("POST", "/v1/utterance", {"text": "打开书房台灯", "session_id": "dup"})
    assert first["kind"] == "executed"
    assert second["kind"] == "deduplicated" and second["dispatch_count"] == 0


def test_cancel_endpoint_releases_nothing(api):
    request, engine = api
    _, parked = request("POST", "/v1/utterance", {"text": "撤防", "session_id": "cancel"})
    _, result = request("POST", "/v1/cancel", {"token": parked["confirmation_token"], "session_id": "cancel"})
    assert result["kind"] == "cancelled"
    assert engine.backend.dispatches == []


def test_audit_endpoint_returns_full_trail(api):
    request, _ = api
    _, decision = request("POST", "/v1/utterance", {"text": "打开客厅大灯", "session_id": "audit"})
    _, rows = request("GET", "/v1/audit", {"trace_id": decision["trace_id"]})
    stages = [row["stage"] for row in rows]
    assert "utterance" in stages and "nlu" in stages and "dispatch" in stages
    dispatch_row = next(row for row in rows if row["stage"] == "dispatch")
    assert dispatch_row["dispatch_state"] == "executed"
    assert dispatch_row["targets"] == ["light.living_room_main"]


def test_settings_default_to_loopback_and_no_cloud():
    settings = make_settings()
    assert settings.server_host in {"127.0.0.1", "localhost"}
    assert settings.allow_cloud_llm is False


def test_fastapi_adapter_routes_the_same_contract():
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"本机 TestClient 不可用（{type(exc).__name__}: {exc}），HTTP 契约已由 handle_request 覆盖")

    settings = Settings(db_path=":memory:", policy_path=POLICY, device_catalog=CATALOG)
    server_module.ENGINE = None
    client = TestClient(server_module.create_app(settings))
    assert client.get("/v1/health").status_code == 200
    parked = client.post("/v1/utterance", json={"text": "解锁入户门锁", "session_id": "adapter"}).json()
    assert parked["kind"] == "awaiting_confirmation"
    assert client.post("/v1/confirm", json={"token": parked["confirmation_token"], "session_id": "adapter"}).json()[
        "kind"
    ] == "executed"
    engine = server_module.ENGINE
    engine.store.close()
    server_module.ENGINE = None
