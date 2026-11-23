"""Application assembly: backend + parser + engine (+ optional ASR/TTS carriers).

Kept separate from :mod:`hvsl.engine` so the HA integration can import the core
without dragging the voice pipeline in, and so tests can build an engine with a
bare minimum of wiring.
"""

from __future__ import annotations

from typing import Any

from .backends.base import DeviceBackend
from .backends.simulated import SimulatedBackend
from .config import Settings
from .engine import DecisionEngine
from .models import Decision
from .risk import RiskPolicy
from .store import Store
from .tools import build_default_registry


def make_parser(settings: Settings, registry, catalog: list[dict[str, str]]):
    """Rule parser by default; the LLM provider degrades to it on any failure."""
    if settings.nlu_provider == "llm":
        from pipeline.nlu import LlmParser, RuleParser

        return LlmParser(
            settings, registry=registry, fallback=RuleParser(registry, catalog=catalog), catalog=catalog
        )
    from pipeline.nlu import RuleParser

    return RuleParser(registry, catalog=catalog)


def build_engine(
    settings: Settings | None = None,
    *,
    backend: DeviceBackend | None = None,
    store: Store | None = None,
) -> DecisionEngine:
    settings = settings or Settings()
    registry = build_default_registry()
    policy = RiskPolicy.load(settings.policy_path)
    if backend is None:
        backend = _default_backend(settings)
    catalog = [
        {"entity_id": e.entity_id, "display_name": e.display_name} for e in backend.entities()
    ]
    parser = make_parser(settings, registry, catalog)

    cross_check = None
    if settings.nlu_provider == "llm" and settings.llm_cross_check:
        from pipeline.nlu import RuleParser

        # 模型提方案，确定性解析负责纠偏（替用户消歧、凭空造设备都不允许）
        cross_check = RuleParser(registry)
    engine = DecisionEngine(
        backend,
        parser,
        settings=settings,
        registry=registry,
        policy=policy,
        store=store or Store(settings.db_path),
        cross_check=cross_check,
    )
    # P0-5：CLI/server 重启后恢复未过期待确认指令（空表时是无操作）。
    engine.gate.restore()
    return engine


def _default_backend(settings: Settings) -> DeviceBackend:
    if settings.ha_base_url and settings.ha_token:
        from .backends.home_assistant import HomeAssistantBackend

        return HomeAssistantBackend(
            settings.ha_base_url,
            settings.ha_token,
            timeout=settings.dispatch_timeout_seconds,
            real_entities=settings.ha_real_entities,
            catalog_path=settings.device_catalog,
        )
    return SimulatedBackend.from_catalog(settings.device_catalog)


class VoiceAssistant:
    """Text/audio in -> decision out -> spoken reply. The demo loop."""

    def __init__(self, engine: DecisionEngine, *, asr=None, tts=None) -> None:
        self.engine = engine
        self.asr = asr
        self.tts = tts

    @classmethod
    def create(cls, settings: Settings | None = None, *, with_asr: bool = False, with_tts: bool = False) -> VoiceAssistant:
        settings = settings or Settings()
        engine = build_engine(settings)
        asr = None
        tts = None
        if with_asr:
            from pipeline.asr import LocalAsr

            asr = LocalAsr(
                python_bin=settings.asr_python or None,
                model_dir=settings.asr_model_dir or None,
                model_size=settings.asr_model_size,
                device=settings.asr_device,
            )
        if with_tts:
            from pipeline.tts import Tts

            tts = Tts(out_path=settings.tts_wav_out)
        return cls(engine, asr=asr, tts=tts)

    def hear(self, audio_path: str, **decode_kwargs) -> Any:
        if self.asr is None:
            raise RuntimeError("ASR 未启用：pip install faster-whisper 或设置 ASR_PYTHON_BIN")
        return self.asr.transcribe(audio_path, **decode_kwargs)

    def say(self, text: str) -> Any:
        if self.tts is None:
            return None
        return self.tts.synthesize(text)

    def speak_decision(self, decision: Decision) -> Decision:
        if self.tts is not None:
            decision.timings_ms["tts"] = self.tts.synthesize(decision.message).wall_seconds * 1000.0
        return decision

    def respond(self, utterance: str, *, session_id: str = "default") -> Decision:
        return self.speak_decision(self.engine.handle(utterance, session_id=session_id))

    def run_audio(self, audio_path: str, *, session_id: str = "default") -> dict[str, Any]:
        result = self.hear(audio_path)
        decision = self.respond(result.text, session_id=session_id)
        return {"asr": result, "decision": decision}
