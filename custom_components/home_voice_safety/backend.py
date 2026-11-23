"""Device backend that runs *inside* Home Assistant (F-002/F-003).

The engine stays synchronous, so HA's service bus is bridged from the executor
thread onto HA's event loop with ``asyncio.run_coroutine_threadsafe`` and the
public coroutine ``hass.services.async_call(..., blocking=True)``. Modern Home
Assistant no longer exposes the old synchronous ``services.call`` publicly, so the
bridge is the only supported dispatch path.

Failure mapping is part of the safety story:

* ``ServiceNotFound`` proves the command never reached a device ->
  :class:`~hvsl.errors.PreDispatchError` (retry is safe);
* the future timing out means the outcome is unknown ->
  :class:`~hvsl.errors.DispatchTimeoutError` (the idempotency guard must not resend).

``hass`` is duck-typed on purpose - it keeps the whole dispatch path testable
without importing ``homeassistant`` (see ``tests/test_ha_async_dispatch.py``), and
it isolates the single point where HA version drift (方案 R3) can bite.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from collections.abc import Iterable
from typing import Any

from hvsl.backends.base import DeviceBackend, resolve_adjust_args
from hvsl.backends.home_assistant import (
    _to_ha_payload,
    build_entity,
    load_catalog_overlay,
)
from hvsl.catalog import load_domain_words
from hvsl.errors import BackendUnavailableError, DispatchTimeoutError, PreDispatchError
from hvsl.models import DeviceEntity
from hvsl.tools import ToolDef


def _as_set(raw: str | Iterable[str] | None) -> set[str]:
    if not raw:
        return set()
    if isinstance(raw, str):
        return {item.strip() for item in raw.split(",") if item.strip()}
    return {str(item) for item in raw}


def _is_service_not_found(exc: BaseException) -> bool:
    """Recognise HA's ``ServiceNotFound`` without importing Home Assistant in tests.

    The class name check also covers duck-typed fakes; the import path keeps real
    HA exceptions (including subclasses) working when the package is installed.
    """
    if exc.__class__.__name__ == "ServiceNotFound":
        return True
    try:
        from homeassistant.exceptions import ServiceNotFound
    except ImportError:
        return False
    return isinstance(exc, ServiceNotFound)


class HassLocalBackend(DeviceBackend):
    kind = "home_assistant_local"

    def __init__(
        self,
        hass: Any,
        *,
        catalog_path: str | None = None,
        real_entities: str | Iterable[str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        if hass is None:
            raise BackendUnavailableError("hass 对象缺失，无法在 HA 内控制设备")
        self._overlay = load_catalog_overlay(catalog_path)
        if not self._overlay:
            # 危险标签来自这份评审过的清单；清单读不到就没有可靠的风险判定，
            # 宁可拒绝启动，也不要“把所有设备都当成安全设备”。
            raise BackendUnavailableError(
                f"读不到已评审的设备清单（catalog_path={catalog_path!r}）。"
                "请把 devices/devices.json 复制到 HA 配置目录并在配置流里填写其绝对路径。"
            )
        self._hass = hass
        self._real = _as_set(real_entities)
        self._timeout = timeout
        self.domain_words = load_domain_words(catalog_path)
        self.dispatches: list[dict[str, Any]] = []

    def entities(self) -> list[DeviceEntity]:
        states = self._hass.states.async_all() or []
        return [
            build_entity(
                state.entity_id,
                getattr(state, "state", "unknown"),
                dict(getattr(state, "attributes", {}) or {}),
                self._overlay.get(state.entity_id),
                self._real,
            )
            for state in states
        ]

    def get(self, entity_id: str) -> DeviceEntity | None:
        state = self._hass.states.get(entity_id)
        if state is None:
            return None
        return build_entity(
            state.entity_id,
            getattr(state, "state", "unknown"),
            dict(getattr(state, "attributes", {}) or {}),
            self._overlay.get(state.entity_id),
            self._real,
        )

    def _resolve_adjust_args(self, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        """Relative adjustments read the current state before touching the bus."""
        if len(entity_ids) != 1:
            raise PreDispatchError("相对调节一次只支持一个设备，未下发")
        entity = self.get(entity_ids[0])
        if entity is None:
            raise PreDispatchError(f"读不到实体 {entity_ids[0]!r} 的当前状态，未下发")
        resolved = resolve_adjust_args(entity, int(args["delta_pct"]))
        return {**resolved, "entity_ids": list(entity_ids)}

    def call_tool(self, tool: ToolDef, entity_ids: list[str], args: dict[str, Any]) -> dict[str, Any]:
        domain, service = tool.ha_service
        if not self._hass.services.has_service(domain, service):
            # Nothing left the process, so the idempotency guard may release the key.
            raise PreDispatchError(f"HA 未注册服务 {domain}.{service}（对应集成可能未启用）")
        if "delta_pct" in args:
            # Resolve against the live state *before* building the payload; a missing
            # state raises PreDispatchError and no service call is made.
            args = self._resolve_adjust_args(entity_ids, args)
        payload = _to_ha_payload(args)
        payload["entity_id"] = entity_ids if len(entity_ids) > 1 else entity_ids[0]

        # 引擎跑在 executor 线程，HA 的事件循环在另一线程：必须用线程安全桥接，
        # 不能在 worker 线程里 await。loop 不可用时命令尚未发出，可安全重试。
        loop = getattr(self._hass, "loop", None)
        if loop is None or loop.is_closed() or not loop.is_running():
            raise PreDispatchError(f"HA 事件循环不可用，{domain}.{service} 未发出")
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._hass.services.async_call(domain, service, payload, blocking=True),
                loop,
            )
        except Exception as exc:  # 连 future 都没建立：调用未发出
            raise BackendUnavailableError(f"HA 服务调用 {domain}.{service} 无法提交：{exc}") from exc

        try:
            future.result(timeout=self._timeout)
        except concurrent.futures.TimeoutError as exc:
            # The service may already have run: outcome unknown, never auto-retry.
            future.cancel()
            raise DispatchTimeoutError(
                f"HA 服务调用 {domain}.{service} 超时（{self._timeout}s），结果未知，未自动重发"
            ) from exc
        except Exception as exc:
            if _is_service_not_found(exc):
                raise PreDispatchError(f"HA 服务 {domain}.{service} 不存在，指令未发出") from exc
            # The call may already have reached the device: outcome unknown, do not retry.
            raise BackendUnavailableError(f"HA 服务调用 {domain}.{service} 失败：{exc}") from exc

        self.dispatches.append({"tool": tool.name, "entity_ids": list(entity_ids), "payload": payload})
        return {"ok": True, "backend": self.kind, "service": f"{domain}.{service}"}
