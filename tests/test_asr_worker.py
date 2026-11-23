"""F-004 ASR worker contract: model discovery stays local, output shape is stable."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def worker():
    spec = importlib.util.spec_from_file_location("asr_worker_under_test", ROOT / "pipeline" / "_asr_worker.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_local_snapshot_dir_is_used_directly(worker, tmp_path):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "model.bin").write_bytes(b"x")
    ref, kwargs = worker.resolve_model_ref("large-v3", str(snapshot))
    assert Path(ref) == snapshot
    assert kwargs == {"local_files_only": True}


def test_hub_cache_layout_is_discovered(worker, tmp_path):
    hub = tmp_path / "hub"
    snapshot = hub / "models--Systran--faster-whisper-large-v3" / "snapshots" / "deadbeef"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x")
    ref, kwargs = worker.resolve_model_ref("large-v3", str(hub))
    assert Path(ref) == snapshot
    assert kwargs == {"local_files_only": True}


def test_unknown_dir_falls_back_to_cache_dir_kwargs(worker, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    ref, kwargs = worker.resolve_model_ref("large-v3", str(empty))
    assert ref == "large-v3"
    assert kwargs["model_dir"] == str(empty)
    assert kwargs["local_files_only"] is True


def test_no_model_dir_means_hf_lookup_only_flag_absent(worker):
    ref, kwargs = worker.resolve_model_ref("large-v3", None)
    assert ref == "large-v3"
    assert kwargs == {}


class Segment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class Info:
    duration = 3.0
    language = "zh"


class FakeModel:
    def __init__(self, strict: bool = False) -> None:
        self.calls: list[dict] = []
        self.audio_seen: list[object] = []
        self.strict = strict

    def transcribe(self, audio, **kwargs):
        self.audio_seen.append(audio)
        self.calls.append(kwargs)
        return iter([Segment(0.0, 1.5, " 打开客厅大灯 "), Segment(1.5, 3.0, "然后撤防")]), Info()


def test_transcribe_returns_the_documented_shape(worker, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(worker, "load_model", lambda config: (model, "cuda", "float16", ["cpu fallback note"]))
    monkeypatch.setattr(worker, "load_audio_window", lambda config: ("<窗口样本数组>", 20.0))
    out = worker.transcribe(
        {
            "audio": "clip.m4a",
            "model_size": "large-v3",
            "device": "cuda",
            "compute_type": "float16",
            "beam_size": 1,
            "vad_filter": True,
            "language": "zh",
            "offset": 12.0,
            "duration": 20.0,
        }
    )
    assert out["text"] == "打开客厅大灯然后撤防"
    assert out["segments"] == [
        {"start": 0.0, "end": 1.5, "text": "打开客厅大灯"},
        {"start": 1.5, "end": 3.0, "text": "然后撤防"},
    ]
    assert out["audio_seconds"] == 20.0, "窗口时长必须来自切片，而不是整条录音"
    assert out["windowed"] is True
    assert out["device"] == "cuda"
    assert out["load_seconds"] is not None and out["load_seconds"] >= 0
    assert out["decode_seconds"] is not None and out["decode_seconds"] >= 0
    assert model.audio_seen == ["<窗口样本数组>"]
    assert model.calls[0]["vad_filter"] is True
    assert "offset" not in model.calls[0], "offset 不能再交给 transcribe（会被忽略而转完整段）"


def test_window_slicing_falls_back_loudly_when_decoder_missing(worker):
    """切片失败必须留下警告，而不是静默把整段转完。"""
    config = {"audio": "clip.wav", "offset": 1.0, "duration": 2.0}
    audio_input, seconds = worker.load_audio_window(config)
    assert audio_input == "clip.wav"
    assert seconds is None
    assert config.get("_warnings"), "退回整段时没有留下警告"
    assert "窗口切片失败" in config["_warnings"][0]


def test_window_slice_uses_sample_arithmetic(worker, monkeypatch):
    """offset/duration 换算成 16k 采样点，并夹在音频真实长度之内。"""
    import sys
    import types

    rate = 16000
    fake_audio = list(range(rate * 60))
    fw = types.ModuleType("faster_whisper")
    fw_audio = types.ModuleType("faster_whisper.audio")
    fw_audio.decode_audio = lambda path, sampling_rate=16000, **kw: fake_audio
    fw.audio = fw_audio
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", fw_audio)

    window, seconds = worker.load_audio_window({"audio": "x.wav", "offset": 10.0, "duration": 20.0})
    assert seconds == 20.0 and len(window) == rate * 20 and window[0] == 10 * rate

    clipped, clipped_seconds = worker.load_audio_window({"audio": "x.wav", "offset": 55.0, "duration": 20.0})
    assert clipped_seconds == 5.0, "窗口超出末尾要夹住，不能报出比音频更长的时长"

    tail, tail_seconds = worker.load_audio_window({"audio": "x.wav", "offset": 50.0})
    assert tail_seconds == 10.0, "只给 offset 时取到音频结束"


def test_window_slice_refuses_an_empty_window_loudly(worker, monkeypatch):
    """offset 落在音频之外：退回整段并留下警告，不能当成“没说话”。"""
    import sys
    import types

    fw = types.ModuleType("faster_whisper")
    fw_audio = types.ModuleType("faster_whisper.audio")
    fw_audio.decode_audio = lambda path, sampling_rate=16000, **kw: list(range(16000 * 3))
    fw.audio = fw_audio
    monkeypatch.setitem(sys.modules, "faster_whisper", fw)
    monkeypatch.setitem(sys.modules, "faster_whisper.audio", fw_audio)

    config = {"audio": "x.wav", "offset": 90.0, "duration": 5.0}
    audio_input, seconds = worker.load_audio_window(config)
    assert audio_input == "x.wav" and seconds is None
    assert "窗口为空" in config["_warnings"][0]


def test_transcribe_carries_window_warnings_through(worker, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(worker, "load_model", lambda config: (model, "cpu", "int8", []))

    def fake_window(config):
        config.setdefault("_warnings", []).append("窗口切片失败，退回整段音频：RuntimeError: boom")
        return config["audio"], None

    monkeypatch.setattr(worker, "load_audio_window", fake_window)
    out = worker.transcribe({"audio": "clip.wav", "offset": 1.0, "duration": 2.0})
    assert out["windowed"] is False
    assert any("窗口切片失败" in warning for warning in out["warnings"])


def test_no_window_means_whole_file(worker, monkeypatch):
    model = FakeModel()
    monkeypatch.setattr(worker, "load_model", lambda config: (model, "cpu", "int8", []))
    out = worker.transcribe({"audio": "clip.wav", "vad_filter": False})
    assert out["windowed"] is False
    assert out["audio_seconds"] == 3.0  # 来自 info.duration
    assert model.audio_seen == ["clip.wav"]


def test_model_load_failure_is_reported(worker, monkeypatch):
    def boom(config):
        raise RuntimeError("cublas64_12.dll is not found")

    monkeypatch.setattr(worker, "load_model", boom)
    with pytest.raises(RuntimeError):
        worker.transcribe({"audio": "clip.wav"})
