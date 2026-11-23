"""Home Assistant entrypoints for the Home Voice Safety Layer integration.

HA imports (and ``.coordinator``/``.config_flow``, which need them) are imported
inside the entrypoint functions on purpose: that keeps
``custom_components.home_voice_safety.{const,safety,backend,services,audit}``
importable without Home Assistant installed, which is how
``tests/test_integration_backend.py`` proves the dispatch path offline.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# The integration ships its own copy of the ``hvsl``/``pipeline`` packages under
# ``vendor/`` so HACS/manual installs never need ``pip install hvsl``.  This must
# run before *any* module in this package imports ``hvsl`` (``config_flow`` and
# ``coordinator`` do it at module import time).  Keep the injection idempotent and
# put the vendor directory first: a stale ``hvsl`` from the host environment must
# never shadow the reviewed copy shipped in this integration.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor"


def _ensure_vendored_core() -> None:
    """Prepend ``vendor/`` to ``sys.path``; fail loudly if it is incomplete."""
    hvsl_pkg = _VENDOR_DIR / "hvsl" / "__init__.py"
    pipeline_pkg = _VENDOR_DIR / "pipeline" / "__init__.py"
    if not hvsl_pkg.is_file() or not pipeline_pkg.is_file():
        raise RuntimeError(
            "内置依赖缺失："
            f"{_VENDOR_DIR} 中找不到 hvsl/pipeline。"
            "请在仓库根目录运行 `python scripts/sync_vendor.py` 重新生成 vendor 后部署集成。"
        )

    vendor = str(_VENDOR_DIR)
    if vendor not in sys.path:
        sys.path.insert(0, vendor)
    elif sys.path[0] != vendor:
        # Already on the path but shadowed by an older package; move it to the front.
        sys.path.remove(vendor)
        sys.path.insert(0, vendor)


try:
    _ensure_vendored_core()
except RuntimeError as exc:  # pragma: no cover - only on a broken vendor copy
    _LOGGER.error("%s", exc)
    raise


def _remove_services(hass, services) -> None:
    """Remove this integration's services without failing on a partial setup."""
    from .const import DOMAIN

    for service in services:
        try:
            hass.services.async_remove(DOMAIN, service)
        except Exception as exc:  # noqa: BLE001 - unload must be best effort
            _LOGGER.debug("移除 service %s.%s 失败（可能未注册）：%s", DOMAIN, service, exc)


def _delete_database_files(db_path: str) -> None:
    """Delete the audit DB only after the engine has closed it."""
    if not db_path or db_path == ":memory:":
        return
    base = Path(db_path)
    for candidate in (base, Path(f"{base}-wal"), Path(f"{base}-shm")):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            _LOGGER.warning("删除审计数据库文件 %s 失败：%s", candidate, exc)


async def async_setup_entry(hass, entry) -> bool:
    import importlib

    from .const import DOMAIN, PLATFORMS

    # HA 2026 会在事件循环中检测 blocking import；hvsl/pydantic 的导入必须
    # 落到 executor，否则每次 setup 都会刷 warning。
    SERVICES: tuple[str, ...] = ()
    config_flow_module = await hass.async_add_executor_job(
        importlib.import_module, f"{__package__}.config_flow"
    )
    coordinator_module = await hass.async_add_executor_job(
        importlib.import_module, f"{__package__}.coordinator"
    )
    services_module = await hass.async_add_executor_job(
        importlib.import_module, f"{__package__}.services"
    )
    async_update_options = config_flow_module.async_update_options
    async_setup_coordinator = coordinator_module.async_setup_coordinator
    SERVICES = services_module.SERVICES
    async_register = services_module.async_register

    coordinator = None
    try:
        coordinator = await async_setup_coordinator(hass, entry)
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
        entry.async_on_unload(entry.add_update_listener(async_update_options))

        async_register(
            hass,
            coordinator.engine,
            notify_service=coordinator.notify_service,
            export_dir=coordinator.export_dir,
        )
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

        _LOGGER.info(
            "已加载 %s：确认有效期 %ss，去重窗口 %ss，危险类别 %s",
            DOMAIN,
            coordinator.engine.gate.ttl_seconds,
            coordinator.engine.idempotency.window_seconds,
            ",".join(coordinator.engine.policy.danger_classes()),
        )
        # P0-5：gate.restore() 已在 coordinator 构造 engine 时执行；这里只把
        # “有多少条待确认指令跨重启存活下来”写进日志，便于真机排查。
        pending_count = len(coordinator.engine.gate)
        if pending_count:
            _LOGGER.info("已恢复 %s 条未过期待确认指令，旧 token 仍可 confirm", pending_count)
        return True
    except Exception:
        # Setup failure must not leave a half-registered coordinator behind, and
        # must not make a later unload raise KeyError on hass.data[DOMAIN].
        entry_map = hass.data.get(DOMAIN)
        if isinstance(entry_map, dict):
            entry_map.pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            _remove_services(hass, SERVICES)
        if coordinator is not None:
            try:
                await hass.async_add_executor_job(coordinator.engine.close)
            except Exception as exc:  # noqa: BLE001 - keep the original setup error
                _LOGGER.warning("setup 失败后关闭 engine 再次失败：%s", exc)
        raise


async def async_unload_entry(hass, entry) -> bool:
    from .const import CONF_REMOVE_DATA, DOMAIN, PLATFORMS
    from .services import SERVICES

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    # Use .get(): an unload can follow a failed setup, when DOMAIN was never
    # created in hass.data.
    entry_map = hass.data.get(DOMAIN)
    coordinator = entry_map.pop(entry.entry_id, None) if isinstance(entry_map, dict) else None
    entry_data = dict(getattr(entry, "data", None) or {})
    entry_options = dict(getattr(entry, "options", None) or {})
    data = {**entry_data, **entry_options}
    remove_data = bool(data.get(CONF_REMOVE_DATA, False))

    db_path = ""
    if coordinator is not None:
        engine = getattr(coordinator, "engine", None)
        settings = getattr(engine, "settings", None)
        db_path = str(getattr(settings, "db_path", "") or "")
        if engine is not None:
            try:
                await hass.async_add_executor_job(engine.close)
            except Exception as exc:  # noqa: BLE001 - unload must not abort on close
                _LOGGER.warning("关闭 decision engine 失败：%s", exc)

        if remove_data:
            if db_path:
                await hass.async_add_executor_job(_delete_database_files, db_path)
                _LOGGER.info("remove_data=true：已删除审计数据库 %s", db_path)
        elif db_path:
            # Explicitly visible, not a silent retention: the operator can decide
            # to remove it on a later uninstall by enabling remove_data.
            _LOGGER.info(
                "已卸载 %s；审计数据库保留在 %s（remove_data=false）。"
                "如需删除，请在卸载前勾选 remove_data。",
                DOMAIN,
                db_path,
            )

    # Multiple entries share the same DOMAIN-level service names. Only the last
    # entry may remove them, otherwise the remaining entry loses its services.
    if not hass.data.get(DOMAIN):
        _remove_services(hass, SERVICES)
    return True


async def async_migrate_entry(hass, entry) -> bool:
    """Additive v1 -> v2 migration: defaults only, never drop user paths/options."""
    from .const import (
        CONFIG_ENTRY_VERSION,
        CONF_DEVICE_CATALOG,
        CONF_NOTIFY_SERVICE,
        CONF_POLICY_PATH,
        CONF_REMOVE_DATA,
        DOMAIN,
    )

    if entry.version == CONFIG_ENTRY_VERSION:
        return True
    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "%s 配置条目版本 %s 高于支持版本 %s（reason=unsupported_future_version），拒绝降级",
            DOMAIN,
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False
    if entry.version != 1:
        _LOGGER.error(
            "%s 配置条目版本 %s 无法识别（reason=unsupported_version），请重新配置",
            DOMAIN,
            entry.version,
        )
        return False

    try:
        data = dict(entry.data or {})
        options = {CONF_REMOVE_DATA: False, CONF_NOTIFY_SERVICE: ""}
        options.update(entry.options or {})

        config_dir = Path(hass.config.config_dir)
        bootstrap = config_dir / "home_voice_safety"

        if not data.get(CONF_DEVICE_CATALOG):
            candidate = bootstrap / "devices.json"
            if not candidate.is_file():
                _LOGGER.error(
                    "%s 迁移 1→2 失败：缺少设备清单路径（reason=migration_missing_catalog）；"
                    "条目保持 v1，可回滚后补路径再试",
                    DOMAIN,
                )
                return False
            data[CONF_DEVICE_CATALOG] = str(candidate)

        if not data.get(CONF_POLICY_PATH):
            candidate = bootstrap / "risk_policy.json"
            if not candidate.is_file():
                candidate = _VENDOR_DIR / "hvsl" / "policy" / "risk_policy.json"
            if not candidate.is_file():
                _LOGGER.error(
                    "%s 迁移 1→2 失败：缺少风险策略路径（reason=migration_missing_policy）；"
                    "条目保持 v1，可回滚后补路径再试",
                    DOMAIN,
                )
                return False
            data[CONF_POLICY_PATH] = str(candidate)

        # Single write after all checks: a failure keeps the entry untouched at v1.
        hass.config_entries.async_update_entry(
            entry, data=data, options=options, version=CONFIG_ENTRY_VERSION
        )
        _LOGGER.info("%s 配置条目已迁移 1→2（默认 options 已补齐）", DOMAIN)
        return True
    except Exception as exc:  # noqa: BLE001 - migration must fail closed/offline
        _LOGGER.error(
            "%s 迁移 1→2 失败（reason=migration_failed）：%s；条目保持 v%s",
            DOMAIN,
            exc,
            entry.version,
        )
        return False
