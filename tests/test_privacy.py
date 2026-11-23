"""F-010 / A7: the decision chain runs with no network at all, and ASR/TTS stay on the machine."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import CATALOG, FixedParser, make_engine, make_settings, step

from hvsl.audit import AuditLog
from hvsl.config import AuditRecord, Settings
from hvsl.models import DecisionKind
from hvsl.store import REDACTED_UTTERANCE, Store
from pipeline import asr as asr_module


class NetworkBlocked(RuntimeError):
    pass


@pytest.fixture()
def no_network(monkeypatch):
    def guard(*args, **kwargs):
        raise NetworkBlocked("本机决策链不得联网")

    monkeypatch.setattr(socket, "socket", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket, "getaddrinfo", guard)
    return guard


def test_the_whole_decision_chain_runs_without_any_socket(no_network):
    engine = make_engine(parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]))
    parked = engine.handle("解锁大门")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    confirmed = engine.confirm(parked.confirmation_token)
    assert confirmed.kind is DecisionKind.EXECUTED
    assert engine.backend.dispatch_count == 1
    engine.store.close()


def test_rule_parser_needs_no_network(no_network):
    from pipeline.nlu import RuleParser

    intent = RuleParser().parse("把客厅大灯亮度调到六十")
    assert intent.steps[0].tool == "hvs.light.set_brightness"
    assert intent.steps[0].args == {"brightness_pct": 60}


def test_asr_worker_is_launched_offline_only(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    class FakeCompleted:
        returncode = 0
        stderr = ""
        stdout = json.dumps(
            {
                "text": "打开客厅大灯",
                "language": "zh",
                "segments": [{"start": 0.0, "end": 1.2, "text": "打开客厅大灯"}],
                "audio_seconds": 1.2,
                "model": "large-v3",
                "device": "cuda",
                "compute_type": "float16",
            },
            ensure_ascii=False,
        )

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        captured["timeout"] = kwargs.get("timeout")
        return FakeCompleted()

    monkeypatch.setattr(asr_module.subprocess, "run", fake_run)
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")

    asr = asr_module.LocalAsr(python_bin=sys.executable, model_dir=str(tmp_path))
    result = asr.transcribe(audio, offset=1.0, duration=2.0)

    env = captured["env"]
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert result.execution == "subprocess"
    assert result.text == "打开客厅大灯"
    assert result.segment_count == 1
    assert isinstance(captured["cmd"], list), "命令必须用列表传参，不能拼 shell 字符串"
    assert str(asr_module.WORKER) in captured["cmd"]
    assert json.loads(captured["cmd"][2])["audio"] == str(audio)


def test_asr_rejects_a_missing_file(tmp_path):
    asr = asr_module.LocalAsr(python_bin=sys.executable)
    with pytest.raises(FileNotFoundError):
        asr.transcribe(tmp_path / "nope.wav")


def test_asr_reports_how_it_will_run(tmp_path, monkeypatch):
    class Probe:
        returncode = 0
        stdout = "1.2.1\n"
        stderr = ""

    monkeypatch.setattr(asr_module.subprocess, "run", lambda *a, **k: Probe())
    monkeypatch.setattr(asr_module, "in_process_available", lambda: False)
    asr = asr_module.LocalAsr(python_bin=sys.executable, model_dir=str(tmp_path))
    info = asr.availability()
    assert info["available"] is True
    assert info["execution"] in {"in-process", "subprocess"}
    if info["execution"] == "subprocess":
        assert info["version"] == "1.2.1"
        assert Path(info["python"]).name in {"python.exe", "python3", "python"}


def test_asr_probe_reports_unavailable_without_raising(tmp_path, monkeypatch):
    class Probe:
        returncode = 1
        stdout = ""
        stderr = "ModuleNotFoundError: No module named 'faster_whisper'"

    monkeypatch.setattr(asr_module.subprocess, "run", lambda *a, **k: Probe())
    monkeypatch.setattr(asr_module, "in_process_available", lambda: False)
    info = asr_module.LocalAsr(python_bin=sys.executable).availability()
    assert info["available"] is False
    assert "faster_whisper" in info["error"]


def test_http_server_refuses_to_bind_beyond_loopback():
    from hvsl.server import run_server

    settings = make_settings().model_copy(update={"server_host": "0.0.0.0"})
    with pytest.raises(SystemExit, match="隐私边界"):
        run_server(settings)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="本机没有 PowerShell SAPI")
def test_tts_synthesis_is_local_and_writes_a_wav(tmp_path, no_network):
    from pipeline.tts import Tts

    tts = Tts(provider="windows-sapi", out_path=tmp_path / "speech.wav")
    assert tts.availability()["local_only"] is True
    result = tts.synthesize("已打开客厅大灯，危险动作需要您确认。")
    assert result.provider == "windows-sapi"
    assert result.audio_bytes > 44, result.note
    assert Path(result.audio_path).is_file()
    assert Path(result.audio_path).read_bytes()[:4] == b"RIFF"
    assert result.spoken is False, "默认不得外放声音"


def test_tts_rejects_overlong_text(tmp_path):
    from pipeline.tts import Tts, TextTooLong

    with pytest.raises(TextTooLong):
        Tts(provider="null", out_path=tmp_path / "x.wav").synthesize("很长" * 400)


def test_tts_null_provider_falls_back_to_text(capsys):
    from pipeline.tts import Tts

    result = Tts(provider="null").synthesize("已执行")
    assert result.audio_path == ""
    assert "文本回读" in result.note
    captured = capsys.readouterr()
    assert "已执行" in captured.err


def test_requirements_do_not_pull_a_cloud_transcribe_service():
    text = Path("requirements.txt").read_text(encoding="utf-8")
    for banned in ("openai", "azure-cognitive", "google-cloud-texttospeech", "baidu-aip"):
        assert banned not in text

# ---------------------------------------------------------------- R12 privacy


def _append_utterance(store: Store, text: str = "解锁入户门锁") -> None:
    AuditLog(store).append(
        AuditRecord(
            trace_id="trace-r12",
            session_id="客厅",
            ts=time.time(),
            stage="utterance",
            utterance=text,
            risk_level="confirm",
            risk_class="door_unlock",
            tool="hvs.lock.unlock",
        )
    )


def test_store_utterance_false_redacts_but_keeps_evidence(tmp_path):
    store = Store(tmp_path / "privacy.db", store_utterance=False)
    _append_utterance(store)

    row = store.query_one("SELECT * FROM audit")
    assert row["utterance"] == REDACTED_UTTERANCE
    assert row["trace_id"] == "trace-r12", "trace 必须保留"
    assert row["risk_class"] == "door_unlock", "风险类别必须保留"
    assert row["risk_level"] == "confirm"
    store.close()


def test_store_utterance_true_keeps_the_original_sentence(tmp_path):
    store = Store(tmp_path / "privacy.db", store_utterance=True)
    _append_utterance(store)
    assert store.query_one("SELECT * FROM audit")["utterance"] == "解锁入户门锁"
    store.close()


def test_store_utterance_defaults_to_true_and_follows_the_env(monkeypatch):
    monkeypatch.delenv("HVSL_STORE_UTTERANCE", raising=False)
    assert Settings().store_utterance is True
    default_store = Store(":memory:")
    _append_utterance(default_store)
    assert default_store.query_one("SELECT * FROM audit")["utterance"] == "解锁入户门锁"
    default_store.close()

    monkeypatch.setenv("HVSL_STORE_UTTERANCE", "0")
    assert Settings.load().store_utterance is False
    env_store = Store(":memory:")
    _append_utterance(env_store)
    assert env_store.query_one("SELECT * FROM audit")["utterance"] == REDACTED_UTTERANCE
    env_store.close()

    monkeypatch.setenv("HVSL_STORE_UTTERANCE", "no")
    assert Settings.load().store_utterance is False


def test_asr_orphan_cleanup_only_removes_old_own_temp_files(tmp_path):
    now = time.time()
    old_own = tmp_path / "hvsl-old.wav"
    old_own.write_bytes(b"RIFF")
    fresh_own = tmp_path / "hvsl-fresh.pcm"
    fresh_own.write_bytes(b"\x00")
    user_audio = tmp_path / "user_recording.wav"
    user_audio.write_bytes(b"RIFF")
    old_other = tmp_path / "other-hvsl-old.wav"
    old_other.write_bytes(b"RIFF")
    wrong_suffix = tmp_path / "hvsl-old.txt"
    wrong_suffix.write_text("x", encoding="utf-8")

    for path in (old_own, user_audio, old_other, wrong_suffix):
        os.utime(path, (now - 2 * asr_module.ORPHAN_TEMP_MAX_AGE_SECONDS,
                        now - 2 * asr_module.ORPHAN_TEMP_MAX_AGE_SECONDS))
    os.utime(fresh_own, (now - 60, now - 60))

    removed = asr_module._cleanup_orphan_temp_files(roots=[tmp_path], now=now)

    assert removed == [old_own]
    assert not old_own.exists()
    assert fresh_own.exists(), "1 小时内的临时文件不能删"
    assert user_audio.exists(), "用户传入/自有音频绝不能删"
    assert old_other.exists(), "只处理 hvsl- 前缀的自己文件"
    assert wrong_suffix.exists(), "只处理 wav/pcm/tmp"


def test_local_asr_init_runs_the_orphan_sweep(monkeypatch):
    calls: list[object] = []
    sentinel = [Path("hvsl-swept.wav")]

    def fake_cleanup():
        calls.append(object())
        return sentinel

    monkeypatch.setattr(asr_module, "_cleanup_orphan_temp_files", fake_cleanup)
    asr = asr_module.LocalAsr(python_bin=sys.executable)
    assert len(calls) == 1
    assert asr.orphan_temp_files_removed == sentinel
