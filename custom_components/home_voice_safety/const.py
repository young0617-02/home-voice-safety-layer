"""Constants for the Home Voice Safety Layer integration (F-002)."""

from __future__ import annotations

DOMAIN = "home_voice_safety"
#: P1-3：配置条目版本。v1 是首个发布布局，v2 增加 remove_data/notify_service 默认值。
CONFIG_ENTRY_VERSION = 2
#: P1-5：与 manifest.json 的 version 保持同步（sensor device_info 用）。
INTEGRATION_VERSION = "0.2.0"

# 与本仓库 hvsl 包保持一致的工具命名空间前缀（HA 2026.9 要求工具名带集成前缀）。
TOOL_NAMESPACE = "hvs"

CONF_POLICY_PATH = "policy_path"
CONF_DEVICE_CATALOG = "device_catalog"
CONF_DANGEROUS_ENTITIES = "dangerous_entities"
CONF_CONFIRM_TTL = "confirm_ttl_seconds"
CONF_DEDUP_WINDOW = "dedup_window_seconds"
CONF_NLU_PROVIDER = "nlu_provider"
CONF_REAL_ENTITIES = "real_entities"
#: P0-6：可选的通知服务（notify.send_message 的 entity_id，例如 notify.mobile_app_x）。
CONF_NOTIFY_SERVICE = "notify_service"
#: P1-3：卸载时是否删除审计数据库；默认 False（保留证据，显式确认才删除）。
CONF_REMOVE_DATA = "remove_data"

DEFAULT_CONFIRM_TTL = 120.0
DEFAULT_DEDUP_WINDOW = 30.0
#: P0-6：确认通知里可被自动化的 action 前缀；blueprint 依赖它。
CONFIRM_ACTION_PREFIX = "HVS_CONFIRM:"
CANCEL_ACTION_PREFIX = "HVS_CANCEL:"

SERVICE_HANDLE_UTTERANCE = "handle_utterance"
SERVICE_CONFIRM_ACTION = "confirm_action"
SERVICE_CANCEL_ACTION = "cancel_action"
SERVICE_LIST_PENDING = "list_pending"
#: P0-6：审计导出（CSV/JSON，默认脱敏用户原话）。
SERVICE_EXPORT_AUDIT = "export_audit"

EVENT_DECISION = f"{DOMAIN}_decision"
#: P0-6：危险动作进入确认队列时触发，自动化/通知可据此提醒用户。
EVENT_CONFIRMATION_REQUIRED = f"{DOMAIN}_confirmation_required"

PLATFORMS = ["sensor"]
