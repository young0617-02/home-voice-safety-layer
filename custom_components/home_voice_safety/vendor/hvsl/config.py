from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

DEFAULT_DB_PATH = "var/decisions.db"
DEFAULT_POLICY_PATH = "hvsl/policy/risk_policy.json"
REPO_ROOT = Path(__file__).resolve().parents[1]


def resolve_relative(path: str | Path) -> str:
    """Resolve repo-relative defaults so the CLI works from any cwd.

    Absolute paths and paths that do not exist under the repo are returned as-is,
    which keeps operator-supplied paths (HA config dir, mounted volumes) working.
    """
    raw = str(path)
    if not raw or raw == ":memory:" or Path(raw).is_absolute():
        return raw
    if Path(raw).exists():
        return raw
    under_repo = REPO_ROOT / raw
    return str(under_repo) if under_repo.exists() else raw


class Settings(BaseModel):
    """Runtime configuration. Loaded from environment variables by ``Settings.load``."""

    db_path: str = DEFAULT_DB_PATH
    policy_path: str = DEFAULT_POLICY_PATH
    device_catalog: str = "devices/devices.json"

    # F-007 confirmation gate
    confirm_ttl_seconds: float = 120.0
    max_pending_actions: int = 8

    # F-008 idempotency
    dedup_window_seconds: float = 30.0
    in_flight_stale_seconds: float = 120.0

    # P2-2 audit retention: 0 = keep forever; negative values are rejected.
    audit_retention_days: int = Field(default=30, ge=0)

    # R12 privacy: when false, the audit table stores "[redacted]" instead of the
    # user's sentence (trace/risk/targets are kept; schema is unchanged).
    store_utterance: bool = True

    # F-006 dispatch / nlu timeouts
    dispatch_timeout_seconds: float = 8.0
    nlu_timeout_seconds: float = 12.0

    nlu_provider: Literal["rule", "llm"] = "rule"
    #: llm 解析时是否用确定性解析做影子纠偏（默认开；关掉后歧义档会退化）
    llm_cross_check: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5:7b"

    # F-010 privacy boundary: audio must never leave the machine.
    allow_cloud_llm: bool = False
    server_host: str = "127.0.0.1"
    server_port: int = 8777

    # Home Assistant backend (F-002/F-003). Empty base_url => simulated backend.
    ha_base_url: str = ""
    ha_token: str = ""
    # Comma-separated entity ids backed by a physical device (F-003 真实/模拟标注).
    ha_real_entities: str = ""

    # F-004 / F-005
    asr_python: str = ""
    asr_model_dir: str = ""
    asr_model_size: str = "large-v3"
    asr_device: str = "cuda"
    tts_wav_out: str = "var/tts_last.wav"

    @model_validator(mode="after")
    def _resolve_paths(self) -> "Settings":
        """Keep repo-relative defaults working from any cwd; leave absolute paths alone."""
        self.db_path = resolve_relative(self.db_path)
        self.policy_path = resolve_relative(self.policy_path)
        self.device_catalog = resolve_relative(self.device_catalog)
        self.tts_wav_out = resolve_relative(self.tts_wav_out)
        return self

    @classmethod
    def load(cls, env_file: str | Path | None = None) -> "Settings":
        """Build settings from ``.env`` (if present) plus the process environment."""
        import os

        try:  # optional dependency; env file parsing degrades to os.environ
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - depends on install extras
            load_dotenv = None  # type: ignore[assignment]

        candidates = [env_file] if env_file else [".env"]
        for candidate in candidates:
            if candidate and Path(candidate).is_file():
                if callable(load_dotenv):
                    load_dotenv(candidate, override=False)
                else:
                    _load_env_manual(candidate)
                break

        def get(key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)

        s = cls()
        return s.model_copy(
            update={
                k: v
                for k, v in {
                    "db_path": resolve_relative(get("HVSL_DB_PATH", s.db_path) or s.db_path),
                    "policy_path": resolve_relative(get("HVSL_POLICY_PATH", s.policy_path) or s.policy_path),
                    "device_catalog": resolve_relative(
                        get("HVSL_DEVICE_CATALOG", s.device_catalog) or s.device_catalog
                    ),
                    "confirm_ttl_seconds": _num(get("HVSL_CONFIRM_TTL_SECONDS"), s.confirm_ttl_seconds),
                    "dedup_window_seconds": _num(get("HVSL_DEDUP_WINDOW_SECONDS"), s.dedup_window_seconds),
                    "audit_retention_days": _retention_days(get("HVSL_AUDIT_RETENTION_DAYS"), s.audit_retention_days),
                    "store_utterance": _bool(get("HVSL_STORE_UTTERANCE"), s.store_utterance),
                    "dispatch_timeout_seconds": _num(get("HVSL_DISPATCH_TIMEOUT_SECONDS"), s.dispatch_timeout_seconds),
                    "nlu_timeout_seconds": _num(get("HVSL_NLU_TIMEOUT_SECONDS"), s.nlu_timeout_seconds),
                    "nlu_provider": get("HVSL_NLU_PROVIDER", s.nlu_provider),
                    "llm_cross_check": _bool(get("HVSL_LLM_CROSS_CHECK"), s.llm_cross_check),
                    "ollama_base_url": get("OLLAMA_BASE_URL", s.ollama_base_url),
                    "ollama_model": get("OLLAMA_MODEL", s.ollama_model),
                    "allow_cloud_llm": _bool(get("HVSL_ALLOW_CLOUD_LLM"), s.allow_cloud_llm),
                    "ha_base_url": get("HA_BASE_URL", s.ha_base_url),
                    "ha_token": get("HA_TOKEN", s.ha_token),
                    "ha_real_entities": get("HA_REAL_ENTITIES", s.ha_real_entities),
                    "asr_python": get("ASR_PYTHON_BIN", s.asr_python),
                    "asr_model_dir": get("ASR_MODEL_DIR", s.asr_model_dir),
                    "asr_device": get("ASR_DEVICE", s.asr_device),
                    "tts_wav_out": get("TTS_WAV_OUT", s.tts_wav_out),
                }.items()
                if v is not None
            }
        )


def _num(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _retention_days(raw: str | None, default: int) -> int:
    """Parse ``HVSL_AUDIT_RETENTION_DAYS`` and reject negatives explicitly.

    ``model_copy(update=...)`` does not re-run field validators, so the environment
    path must enforce the same ``ge=0`` rule as direct construction.
    """
    try:
        value = int(raw) if raw not in (None, "") else int(default)
    except ValueError:
        value = int(default)
    if value < 0:
        raise ValueError("HVSL_AUDIT_RETENTION_DAYS 不能为负数（0 = 永久保留）")
    return value


def _bool(raw: str | None, default: bool) -> bool:
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_env_manual(path: str | Path) -> None:
    """Minimal ``KEY=value`` parser used when python-dotenv is unavailable."""
    import os

    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class AuditRecord(BaseModel):
    """One row of the F-009 decision trail."""

    trace_id: str
    seq: int = 0
    session_id: str = "default"
    ts: float
    stage: Literal["utterance", "nlu", "dispatch", "result", "confirm", "cancel", "reject", "clarify"]
    utterance: str = ""
    intent: str = ""
    tool: str = ""
    targets: list[str] = Field(default_factory=list)
    risk_level: str = ""
    risk_class: str = ""
    confirmation_state: str = ""
    dispatch_state: str = ""
    idempotency_key: str = ""
    dedup: bool = False
    nlu_provider: str = ""
    degraded: bool = False
    latency_ms: float | None = None
    detail: dict = Field(default_factory=dict)
    error: str = ""
