"""Local HTTP interface (F-010: loopback only by default).

    python -m hvsl.cli serve

``handle_request`` is the framework-agnostic core: it holds the whole API contract
and is what the tests exercise. ``create_app`` is a thin FastAPI adapter on top of
it, so a reviewer without fastapi installed still gets the contract proven.
"""

from __future__ import annotations

from typing import Any, Callable

from .app import build_engine
from .config import Settings

ENGINE: Any = None

Handler = Callable[[dict[str, Any]], tuple[int, Any]]


def get_engine(settings: Settings | None = None):
    global ENGINE
    if ENGINE is None:
        ENGINE = build_engine(settings or Settings.load())
    return ENGINE


def _device_row(entity) -> dict[str, Any]:
    return {
        "entity_id": entity.entity_id,
        "display_name": entity.display_name,
        "domain": entity.domain,
        "state": entity.state,
        "provenance": entity.provenance.value,
        "tags": entity.tags,
    }


def routes(engine) -> dict[tuple[str, str], Handler]:
    """The API surface. Everything dangerous goes through the same gate as the CLI."""

    def health(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, {"ok": True, "backend": engine.backend.kind, "entities": len(engine.backend.entities())}

    def devices(body: dict[str, Any]) -> tuple[int, Any]:
        return 200, [_device_row(entity) for entity in engine.backend.entities()]

    def utterance(body: dict[str, Any]) -> tuple[int, Any]:
        text = str(body.get("text") or "")
        session_id = str(body.get("session_id") or "default")
        return 200, engine.handle(text, session_id=session_id).as_dict()

    def confirm(body: dict[str, Any]) -> tuple[int, Any]:
        token = body.get("token") or None
        return 200, engine.confirm(str(token) if token else None, session_id=str(body.get("session_id") or "default")).as_dict()

    def cancel(body: dict[str, Any]) -> tuple[int, Any]:
        token = body.get("token") or None
        return 200, engine.cancel(str(token) if token else None, session_id=str(body.get("session_id") or "default")).as_dict()

    def audit(body: dict[str, Any]) -> tuple[int, Any]:
        trace_id = body.get("trace_id")
        limit = int(body.get("limit") or 50)
        records = engine.trace(str(trace_id)) if trace_id else engine.audit.query(limit=limit)
        return 200, [record.model_dump() for record in records]

    def pending(body: dict[str, Any]) -> tuple[int, Any]:
        session_id = str(body.get("session_id") or "default")
        return 200, [action.model_dump() for action in engine.pending(session_id)]

    return {
        ("GET", "/v1/health"): health,
        ("GET", "/v1/devices"): devices,
        ("GET", "/v1/audit"): audit,
        ("GET", "/v1/pending"): pending,
        ("POST", "/v1/utterance"): utterance,
        ("POST", "/v1/confirm"): confirm,
        ("POST", "/v1/cancel"): cancel,
    }


def handle_request(
    engine, method: str, path: str, body: dict[str, Any] | None = None
) -> tuple[int, Any]:
    handler = routes(engine).get((method.upper(), path))
    if handler is None:
        return 404, {"error": f"no route for {method} {path}", "available": sorted(p for _, p in routes(engine))}
    return handler(dict(body or {}))


def create_app(settings: Settings | None = None):
    try:
        from fastapi import FastAPI
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise RuntimeError("serve 需要 fastapi：pip install -r requirements.txt") from exc

    class UtteranceIn(BaseModel):
        text: str
        session_id: str = "default"

    class ActionIn(BaseModel):
        token: str | None = None
        session_id: str = "default"

    engine = get_engine(settings)
    app = FastAPI(title="Home Voice Safety Layer", version="0.1.0")

    def respond(method: str, path: str, body: dict[str, Any]) -> Any:
        status, content = handle_request(engine, method, path, body)
        return content

    @app.get("/v1/health")
    def health() -> Any:
        return respond("GET", "/v1/health", {})

    @app.get("/v1/devices")
    def devices() -> Any:
        return respond("GET", "/v1/devices", {})

    @app.get("/v1/pending")
    def pending(session_id: str = "default") -> Any:
        return respond("GET", "/v1/pending", {"session_id": session_id})

    @app.get("/v1/audit")
    def audit(trace_id: str | None = None, limit: int = 50) -> Any:
        return respond("GET", "/v1/audit", {"trace_id": trace_id, "limit": limit})

    @app.post("/v1/utterance")
    def utterance(payload: UtteranceIn) -> Any:
        return respond("POST", "/v1/utterance", payload.model_dump())

    @app.post("/v1/confirm")
    def confirm(payload: ActionIn) -> Any:
        return respond("POST", "/v1/confirm", payload.model_dump())

    @app.post("/v1/cancel")
    def cancel(payload: ActionIn) -> Any:
        return respond("POST", "/v1/cancel", payload.model_dump())

    return app


def run_server(settings: Settings | None = None) -> None:  # pragma: no cover - manual entry
    # 先做 loopback 校验，再加载可选依赖：安全边界不能依赖 uvicorn 是否安装。
    settings = settings or Settings.load()
    host = settings.server_host
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit(
            f"refusing to bind {host}: 本项目的接口默认只听本机（隐私边界 F-010）。"
            "确实要对外暴露请先改掉 HVSL_SERVER_HOST 并自行加认证。"
        )
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("serve 需要 uvicorn：pip install -r requirements.txt") from exc
    uvicorn.run(create_app(settings), host=host, port=settings.server_port, log_level="info")
