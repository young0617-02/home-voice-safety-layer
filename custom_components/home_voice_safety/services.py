"""Service surface (F-002/F-006, P0-4/P0-6): the five HA services of the integration.

``dispatch_request`` is pure and engine-only, so the whole service contract is
testable without Home Assistant; ``async_register`` is the thin HA glue.

Contract highlights:

* every service returns the engine result (``supports_response=OPTIONAL``);
* ``ok`` includes ``partial`` - a dispatch that reached the device but failed is
  not a rejected command;
* ``list_pending`` aggregates every session unless ``session_id`` is given;
* a dangerous action fires ``home_voice_safety_confirmation_required`` with the
  token for automations, and optionally sends a mobile notification whose action
  string is ``HVS_CONFIRM:<token>:<session_id>`` (token only ever lives inside the
  event/notification, never in ``spoken_message``);
* ``export_audit`` writes a redacted CSV/JSON snapshot into the config directory.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

from hvsl.audit import export_records
from hvsl.models import token_hash

from .const import (
    CANCEL_ACTION_PREFIX,
    CONFIRM_ACTION_PREFIX,
    DOMAIN,
    EVENT_CONFIRMATION_REQUIRED,
    EVENT_DECISION,
    SERVICE_CANCEL_ACTION,
    SERVICE_CONFIRM_ACTION,
    SERVICE_EXPORT_AUDIT,
    SERVICE_HANDLE_UTTERANCE,
    SERVICE_LIST_PENDING,
)

_LOGGER = logging.getLogger(__name__)

#: P2-2：审计保留期清理。服务名放在这里而不是 const.py，避免触碰正在并行修改的常量文件。
SERVICE_PURGE_AUDIT = "purge_audit"

SERVICES = (
    SERVICE_HANDLE_UTTERANCE,
    SERVICE_CONFIRM_ACTION,
    SERVICE_CANCEL_ACTION,
    SERVICE_LIST_PENDING,
    SERVICE_EXPORT_AUDIT,
    SERVICE_PURGE_AUDIT,
)

#: Decision kinds that mean "the request was accepted" (partial included).
_OK_KINDS = frozenset(
    {
        "executed",
        "awaiting_confirmation",
        "clarification_required",
        "cancelled",
        "deduplicated",
        "partial",
    }
)


def _supports_response_optional() -> Any:
    """``SupportsResponse.OPTIONAL`` with an offline-test fallback."""
    try:
        from homeassistant.core import SupportsResponse
    except ImportError:  # pragma: no cover - only when HA is not installed
        return "optional"
    return SupportsResponse.OPTIONAL


def _schema_for(service: str) -> Any:
    """Per-service ``vol.Schema``; ``None`` where voluptuous is unavailable/faked (tests)."""
    try:
        import voluptuous as vol

        session = vol.Optional("session_id", default="default")
        schemas = {
            SERVICE_HANDLE_UTTERANCE: vol.Schema({vol.Required("text"): str, session: str}),
            SERVICE_CONFIRM_ACTION: vol.Schema(
                {vol.Optional("token"): vol.Any(None, str), session: str}
            ),
            SERVICE_CANCEL_ACTION: vol.Schema(
                {vol.Optional("token"): vol.Any(None, str), session: str}
            ),
            SERVICE_LIST_PENDING: vol.Schema({vol.Optional("session_id"): vol.Any(None, str)}),
            SERVICE_EXPORT_AUDIT: vol.Schema(
                {
                    vol.Optional("trace_id", default=""): str,
                    vol.Optional("since"): vol.Coerce(float),
                    vol.Optional("until"): vol.Coerce(float),
                    vol.Optional("format", default="csv"): vol.In(["csv", "json"]),
                    vol.Optional("redact_utterance", default=True): bool,
                }
            ),
            SERVICE_PURGE_AUDIT: vol.Schema(
                {
                    vol.Optional("before"): vol.Coerce(float),
                    vol.Optional("days"): vol.All(vol.Coerce(int), vol.Range(min=0)),
                }
            ),
        }
        return schemas.get(service)
    except (ImportError, AttributeError, TypeError):
        # 离线测试环境可能没有 voluptuous，或另一个测试注入了简化版 stub。
        return None


def pending_view(engine, session_id: str | None = None) -> list[dict[str, Any]]:
    """Pending actions; ``session_id=None`` aggregates every session (P0-4)."""
    now = time.time()
    pendings = engine.gate.all(now=now) if session_id is None else engine.gate.for_session(session_id, now=now)
    return [
        {
            "token": pending.token,
            "utterance": pending.utterance,
            "risk_class": pending.risk_class,
            "session_id": pending.session_id,
            "targets": [target for step in pending.steps for target in step.targets],
            "tools": [step.tool for step in pending.steps],
            "trace_id": pending.trace_id,
            "expires_at": pending.expires_at,
            "expires_in_seconds": round(max(0.0, pending.expires_at - now), 1),
        }
        for pending in pendings
    ]


def _decision_result(engine, decision) -> dict[str, Any]:
    """Service response: ``ok``/``partial`` plus the engine decision."""
    kind = decision.kind.value
    result: dict[str, Any] = {
        "ok": kind in _OK_KINDS,
        "partial": kind == "partial",
        **decision.as_dict(),
    }
    if kind == "awaiting_confirmation" and decision.confirmation_token:
        pending = engine.gate.get(decision.confirmation_token)
        if pending is not None:
            # 给自动化/通知用的确认上下文；token 只在这里与事件里出现。
            result["confirmation"] = {
                "token": pending.token,
                "session_id": pending.session_id,
                "risk_class": pending.risk_class,
                "targets": [target for step in pending.steps for target in step.targets],
                "expires_at": pending.expires_at,
                "spoken_message": decision.spoken_message,
            }
    return result


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _export_audit(engine, data: dict[str, Any], export_dir: str | Path | None) -> dict[str, Any]:
    fmt = str(data.get("format") or "csv").lower()
    if fmt not in {"csv", "json"}:
        return {"ok": False, "kind": "invalid_argument", "message": "format 只能是 csv 或 json"}
    if export_dir is None:
        return {"ok": False, "kind": "invalid_argument", "message": "未配置导出目录"}

    redact_raw = data.get("redact_utterance")
    redact = True if redact_raw is None else bool(redact_raw)
    trace_id = str(data.get("trace_id") or "").strip() or None
    records = engine.audit.query_for_export(
        trace_id=trace_id,
        since=_as_float(data.get("since")),
        until=_as_float(data.get("until")),
        limit=100000,
    )
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = trace_id[:12] if trace_id else "all"
    destination = Path(export_dir) / f"audit-{stamp}-{suffix}.{fmt}"
    rows = export_records(records, destination, fmt=fmt, redact_utterance=redact)
    return {
        "ok": True,
        "kind": "exported",
        "format": fmt,
        "rows": rows,
        "path": str(destination),
        "redacted": redact,
    }


def _purge_audit(engine, data: dict[str, Any]) -> dict[str, Any]:
    """P2-2：删除超过保留期的 audit 行；unknown 证据与 pending/idempotency 永不删除。"""
    before_raw = data.get("before")
    days_raw = data.get("days")
    if before_raw not in (None, ""):
        before = float(before_raw)
        resolved_days: int | None = None
    else:
        try:
            days = int(days_raw) if days_raw not in (None, "") else int(engine.settings.audit_retention_days)
        except (TypeError, ValueError):
            return {"ok": False, "kind": "invalid_argument", "message": "days 必须是非负整数"}
        if days < 0:
            return {"ok": False, "kind": "invalid_argument", "message": "days 不能为负数"}
        resolved_days = days
        if days == 0:
            # 0 = 永久保留；不能因为调用一次清理就删历史。
            return {"ok": True, "kind": "purged", "deleted": 0, "before": None, "retention_days": 0}
        before = time.time() - days * 86400.0
    deleted = engine.store.purge(before)
    return {
        "ok": True,
        "kind": "purged",
        "deleted": deleted,
        "before": before,
        "retention_days": resolved_days,
    }


def dispatch_request(
    engine,
    service: str,
    data: dict[str, Any],
    *,
    export_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Map one service call onto the decision engine. Never dispatches anything dangerous itself."""
    session_id = str(data.get("session_id") or "default")
    if service == SERVICE_HANDLE_UTTERANCE:
        decision = engine.handle(str(data.get("text") or ""), session_id=session_id)
    elif service == SERVICE_CONFIRM_ACTION:
        token = data.get("token") or None
        decision = engine.confirm(str(token) if token else None, session_id=session_id)
    elif service == SERVICE_CANCEL_ACTION:
        token = data.get("token") or None
        decision = engine.cancel(str(token) if token else None, session_id=session_id)
    elif service == SERVICE_LIST_PENDING:
        raw_session = data.get("session_id")
        only_session = str(raw_session).strip() if raw_session not in (None, "") else None
        return {"ok": True, "pending": pending_view(engine, only_session)}
    elif service == SERVICE_EXPORT_AUDIT:
        return _export_audit(engine, data, export_dir)
    elif service == SERVICE_PURGE_AUDIT:
        return _purge_audit(engine, data)
    else:
        return {
            "ok": False,
            "kind": "unknown_service",
            "message": f"未注册的服务 {service}；可用：{', '.join(SERVICES)}",
        }
    return _decision_result(engine, decision)


def _safe_event_payload(service: str, result: dict[str, Any]) -> dict[str, Any]:
    """Decision event: strip raw token and the ``confirmation`` block (not for the bus)."""
    payload = {key: value for key, value in result.items() if key != "confirmation"}
    raw_token = payload.pop("confirmation_token", None)
    if raw_token:
        payload["confirmation_token_hash"] = token_hash(raw_token)
    return {"service": service, **payload}


async def _async_notify_confirmation(hass, notify_service: str, confirmation: dict[str, Any]) -> None:
    """Send the mobile-app confirmation prompt; a notify failure must not break the gate."""
    token = str(confirmation.get("token") or "")
    session_id = str(confirmation.get("session_id") or "default")
    target = notify_service if "." in notify_service else f"notify.{notify_service}"
    data = {
        "entity_id": target,
        "title": "Home Voice Safety：需要确认",
        "message": confirmation.get("spoken_message") or "有危险动作需要你确认。",
        "data": {
            "tag": f"hvs-{token}",
            "actions": [
                {"action": f"{CONFIRM_ACTION_PREFIX}{token}:{session_id}", "title": "确认执行"},
                {"action": f"{CANCEL_ACTION_PREFIX}{token}:{session_id}", "title": "取消"},
            ],
        },
    }
    try:
        await hass.services.async_call("notify", "send_message", data, blocking=False)
    except Exception as exc:  # noqa: BLE001 - 通知失败不能影响确认门禁
        # legacy/rest 平台常不接受 mobile_app 的 data(tag/action)，降级纯文本；
        # action/tag 只在 mobile_app 上可用，但“能收到一条文本提醒”仍优于完全静默。
        _LOGGER.warning("发送确认通知失败（带 action），降级纯文本：%s", exc)
        fallback = {key: value for key, value in data.items() if key != "data"}
        try:
            await hass.services.async_call("notify", "send_message", fallback, blocking=False)
        except Exception as fallback_exc:  # noqa: BLE001
            _LOGGER.warning("发送确认通知失败（纯文本）：%s", fallback_exc)


def _schedule_daily_purge(hass, engine) -> None:
    """P2-2：每日执行一次保留期清理；离线测试环境没有 HA 事件助手时跳过。"""
    try:
        from homeassistant.helpers.event import async_track_time_interval
    except ImportError:
        return

    previous = getattr(engine, "_hvs_purge_unsub", None)
    if callable(previous):
        try:
            previous()
        except Exception:  # noqa: BLE001 - 旧定时器取消失败不应阻断注册
            pass

    async def _daily(_now) -> None:
        await hass.async_add_executor_job(
            lambda: dispatch_request(engine, SERVICE_PURGE_AUDIT, {})
        )

    try:
        engine._hvs_purge_unsub = async_track_time_interval(hass, _daily, timedelta(hours=24))
    except Exception as exc:  # noqa: BLE001 - 定时器启用失败不应让集成不可用
        _LOGGER.warning("审计保留期定时任务未启用：%s", exc)


def async_register(
    hass,
    engine,
    *,
    notify_service: str | None = None,
    export_dir: str | Path | None = None,
) -> None:  # noqa: N802 - HA naming
    """Register the services. ``hass`` is duck-typed (services/bus/async_add_executor_job)."""
    supports_response = _supports_response_optional()

    def _make(service: str):
        async def _handler(call) -> dict[str, Any]:
            data = dict(call.data or {})
            result = await hass.async_add_executor_job(
                lambda: dispatch_request(engine, service, data, export_dir=export_dir)
            )
            hass.bus.async_fire(EVENT_DECISION, _safe_event_payload(service, result))
            confirmation = result.get("confirmation")
            if confirmation:
                hass.bus.async_fire(
                    EVENT_CONFIRMATION_REQUIRED,
                    {"service": service, **confirmation},
                )
                if notify_service:
                    coro = _async_notify_confirmation(hass, notify_service, confirmation)
                    # 通知是可选增强：异步发送，慢/失败的 notify 平台不能阻塞确认事件。
                    if hasattr(hass, "async_create_task"):
                        hass.async_create_task(coro)
                    else:
                        await coro
            return result

        return _handler

    for service in SERVICES:
        hass.services.async_register(
            DOMAIN,
            service,
            _make(service),
            schema=_schema_for(service),
            supports_response=supports_response,
        )    # 不改 __init__.py：在服务注册的同一个入口启动每日保留期清理。
    _schedule_daily_purge(hass, engine)