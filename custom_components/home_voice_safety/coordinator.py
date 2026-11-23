"""Coordinator (F-003): owns the decision engine inside HA and publishes snapshots.

The engine is synchronous and SQLite-backed, so every call into it goes through
``hass.async_add_executor_job``.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from hvsl.config import Settings
from hvsl.engine import DecisionEngine
from hvsl.store import Store

from .backend import HassLocalBackend
from .const import (
    CONF_CONFIRM_TTL,
    CONF_DEDUP_WINDOW,
    CONF_DEVICE_CATALOG,
    CONF_NLU_PROVIDER,
    CONF_NOTIFY_SERVICE,
    CONF_POLICY_PATH,
    CONF_REAL_ENTITIES,
    DOMAIN,
    EVENT_DECISION,
    SERVICE_CANCEL_ACTION,
    SERVICE_CONFIRM_ACTION,
    SERVICE_HANDLE_UTTERANCE,
)
from .safety import RiskPolicy, build_default_registry
from .services import dispatch_request, pending_view

_LOGGER = logging.getLogger(__name__)

HomeVoiceSafetyConfigEntry: TypeAlias = ConfigEntry["HomeVoiceSafetyCoordinator"]

# 决策层的默认策略文件随包发布；HA 里也可以用选项覆盖成 /config 下的评审版本。
BUNDLED_POLICY = Path(__file__).resolve().parents[2] / "hvsl" / "policy" / "risk_policy.json"

#: P1-1：轮询只做兜底（事件驱动为主），实体清单快照缓存 30s。
POLL_INTERVAL_SECONDS = 300
ENTITIES_CACHE_SECONDS = 30.0


class HomeVoiceSafetyCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Holds the engine; refreshes entity/pending snapshots for the sensors."""

    config_entry: HomeVoiceSafetyConfigEntry

    def __init__(self, hass: HomeAssistant, entry: HomeVoiceSafetyConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=POLL_INTERVAL_SECONDS),
        )
        self._entry = entry
        self.notify_service: str | None = None
        self._entities_cache: list[Any] | None = None
        self._entities_cached_at = 0.0
        self._refresh_pending = False
        # engine 构造会读 policy/catalog（blocking I/O），必须在 executor 里做。
        self.engine = None  # type: ignore[assignment]
        self._unsub_decision = None

    async def async_setup(self) -> None:
        self.engine = await self.hass.async_add_executor_job(self._build_engine)
        # P1-1：服务调用完成即刷新；300s 轮询只是在事件丢失时的兜底。
        unsub = self.hass.bus.async_listen(EVENT_DECISION, self._handle_decision_event)
        self._unsub_decision = unsub
        if hasattr(self._entry, "async_on_unload"):
            self._entry.async_on_unload(unsub)

    def _build_engine(self) -> DecisionEngine:
        data = {**self._entry.data, **self._entry.options}
        # P0-6：可选通知服务（notify.send_message 的 entity_id）。
        self.notify_service = str(data.get(CONF_NOTIFY_SERVICE) or "").strip() or None
        config_dir = Path(self.hass.config.config_dir)
        catalog = data.get(CONF_DEVICE_CATALOG) or str(config_dir / f"custom_components/{DOMAIN}/devices.json")
        policy_path = data.get(CONF_POLICY_PATH) or str(BUNDLED_POLICY)

        settings = Settings(
            db_path=str(config_dir / f"{DOMAIN}_decisions.db"),
            policy_path=policy_path,
            device_catalog=catalog,
            confirm_ttl_seconds=float(data.get(CONF_CONFIRM_TTL, 120.0)),
            dedup_window_seconds=float(data.get(CONF_DEDUP_WINDOW, 30.0)),
            nlu_provider=data.get(CONF_NLU_PROVIDER, "rule"),
            ha_real_entities=data.get(CONF_REAL_ENTITIES, ""),
        )
        backend = HassLocalBackend(
            self.hass,
            catalog_path=catalog,
            real_entities=settings.ha_real_entities,
            timeout=settings.dispatch_timeout_seconds,
        )
        registry = build_default_registry()
        # 一次构造只读一次真实实体清单（P1-1）。
        entities = backend.entities()
        entity_catalog = [
            {"entity_id": entity.entity_id, "display_name": entity.display_name}
            for entity in entities
        ]
        parser, cross_check = self._make_parser(settings, registry, entity_catalog)
        engine = DecisionEngine(
            backend,
            parser,
            settings=settings,
            registry=registry,
            policy=RiskPolicy.load(settings.policy_path),
            store=Store(settings.db_path),
            cross_check=cross_check,
            unknown_event=self._fire_dispatch_unknown,
        )
        # P0-5：重启后把未过期的待确认指令放回内存；token 不会静默失效。
        restored = engine.gate.restore()
        if restored:
            _LOGGER.info("已恢复 %s 条未过期待确认指令（重启不丢失）", restored)
        _LOGGER.info(
            "home_voice_safety 决策层就绪：实体 %s 个（真实 %s 台），危险类别 %s",
            len(entities),
            sum(1 for entity in entities if entity.provenance.value == "real"),
            ",".join(engine.policy.danger_classes()),
        )
        return engine

    def _make_parser(
        self, settings: Settings, registry, entity_catalog: list[dict[str, str]]
    ) -> tuple[Any, Any]:
        """Return ``(parser, cross_check)``.

        F3：HA 的 LLM 模式必须同时装配确定性影子解析器，否则
        ``DecisionEngine`` 的纠偏分支整体不会启用（模型可以自由替用户消歧）。
        规则模式下 parser 本身就是确定性来源，``cross_check=None``。
        """
        if settings.nlu_provider == "llm":
            from pipeline.nlu import LlmParser, RuleParser

            parser = LlmParser(
                settings, registry=registry, fallback=RuleParser(registry), catalog=entity_catalog
            )
            cross_check = RuleParser(registry) if settings.llm_cross_check else None
            return parser, cross_check
        from pipeline.nlu import RuleParser

        return RuleParser(registry), None

    def _fire_dispatch_unknown(self, name: str, payload: dict[str, Any]) -> None:
        """engine 跑在 executor 线程，事件必须回到 HA 事件循环再 fire。"""
        loop = getattr(self.hass, "loop", None)
        if loop is None:
            return
        loop.call_soon_threadsafe(self.hass.bus.async_fire, name, payload)

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            return await self.hass.async_add_executor_job(self.snapshot)
        except Exception as exc:
            raise UpdateFailed(f"设备清单刷新失败：{exc}") from exc

    def _handle_decision_event(self, _event: Any) -> None:
        """P1-1：服务调用事件 -> 立即刷新；合并同一轮多个事件，避免任务风暴。"""
        if self._refresh_pending:
            return
        self._refresh_pending = True
        loop = getattr(self.hass, "loop", None)
        if loop is None:
            self.hass.async_create_task(self.async_refresh_from_event())
            return
        # 事件回调可能在 executor 线程触发；HA 2026 要求 async_create_task
        # 必须在事件循环线程调用，否则直接 RuntimeError。
        loop.call_soon_threadsafe(
            lambda: self.hass.async_create_task(self.async_refresh_from_event())
        )

    async def async_refresh_from_event(self) -> None:
        """Read a fresh snapshot off the event loop and publish it to the sensors."""
        try:
            # 设备状态可能刚变化；事件驱动这一轮强制重读实体清单。
            self._entities_cached_at = 0.0
            data = await self.hass.async_add_executor_job(self.snapshot)
            self.async_set_updated_data(data)
        finally:
            self._refresh_pending = False

    def _entities_snapshot(self) -> list[Any]:
        """30s 缓存：一次 snapshot（以及并发 sensor 读取）只调用一次 entities()。"""
        now = time.time()
        if self._entities_cache is not None and now - self._entities_cached_at < ENTITIES_CACHE_SECONDS:
            return self._entities_cache
        assert self.engine is not None
        entities = self.engine.backend.entities()
        self._entities_cache = entities
        self._entities_cached_at = now
        return entities

    def snapshot(self) -> dict[str, Any]:
        assert self.engine is not None
        entities = self._entities_snapshot()
        return {
            "entities": len(entities),
            "real_devices": sum(1 for entity in entities if entity.provenance.value == "real"),
            "simulated_devices": sum(1 for entity in entities if entity.provenance.value != "real"),
            "dangerous_entities": sum(1 for entity in entities if entity.is_danger_tagged),
            # P0-4：默认聚合全部会话（多音箱/多房间时不再只看到 default）。
            "pending": pending_view(self.engine),
            # P1-1：聚合 SQL（GROUP BY），不再把十万行 audit 读进内存。
            "stats": self.engine.stats(),
        }

    @property
    def export_dir(self) -> Path:
        """Where ``export_audit`` writes CSV/JSON snapshots (P0-6)."""
        return Path(self.hass.config.config_dir) / DOMAIN / "exports"

    # 供 __init__.py 与 sensor 使用的同步入口（已在 executor 线程里）
    def handle_text(self, text: str, session_id: str = "default") -> dict[str, Any]:
        return dispatch_request(self.engine, SERVICE_HANDLE_UTTERANCE, {"text": text, "session_id": session_id})

    def confirm_token(self, token: str | None, session_id: str = "default") -> dict[str, Any]:
        return dispatch_request(self.engine, SERVICE_CONFIRM_ACTION, {"token": token, "session_id": session_id})

    def cancel_token(self, token: str | None, session_id: str = "default") -> dict[str, Any]:
        return dispatch_request(self.engine, SERVICE_CANCEL_ACTION, {"token": token, "session_id": session_id})

    async def async_handle(self, text: str, session_id: str = "default") -> dict[str, Any]:
        return await self.hass.async_add_executor_job(self.handle_text, text, session_id)

    async def async_confirm(self, token: str | None, session_id: str = "default") -> dict[str, Any]:
        return await self.hass.async_add_executor_job(self.confirm_token, token, session_id)


async def async_setup_coordinator(hass: HomeAssistant, entry: HomeVoiceSafetyConfigEntry) -> HomeVoiceSafetyCoordinator:
    coordinator = HomeVoiceSafetyCoordinator(hass, entry)
    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    return coordinator
