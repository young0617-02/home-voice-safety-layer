from __future__ import annotations

from .base import DeviceBackend
from .home_assistant import HomeAssistantBackend
from .simulated import SimulatedBackend, load_catalog

__all__ = ["DeviceBackend", "SimulatedBackend", "HomeAssistantBackend", "load_catalog", "build_backend"]


def build_backend(settings, catalog_path: str | None = None):
    """Pick the HA backend when configured, otherwise the deterministic simulator."""
    if getattr(settings, "ha_base_url", "") and getattr(settings, "ha_token", ""):
        return HomeAssistantBackend(
            settings.ha_base_url,
            settings.ha_token,
            timeout=settings.dispatch_timeout_seconds,
            real_entities=getattr(settings, "ha_real_entities", None),
        )
    return SimulatedBackend.from_catalog(catalog_path or settings.device_catalog)
