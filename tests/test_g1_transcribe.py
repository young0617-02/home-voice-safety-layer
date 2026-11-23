"""G1 转写脚本测试：全部使用 ``tmp_path`` + fake transcriber，不加载任何模型。

本机未安装 faster-whisper，测试通过注入 ``transcriber_factory`` 把 ASR 换成
确定性 fake，因此既不联网、也不依赖模型缓存。WAV 用低位采样率生成"短/长"
静音，保证超长拒绝路径可测且不写大文件。
"""

from __future__ import annotations

import json
import sys
import wave
from pathlib import Path
from typing import Any, Callable

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import g1_transcribe  # noqa: E402
from pipeline.asr import AsrResult, AsrSegment  # noqa: E402

RATE = 1000  # 采样率取低位：仅用于构造短/长静音，避免测试写几十 MB 的 WAV


def write_wav(path: Path, seconds: float, rate: int = RATE) -> Path:
    frames = max(1, int(round(seconds * rate)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * frames)
    return path


class FakeTranscriber:
    """确定性 fake：记录调用路径，可选对指定文件名抛异常。"""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.calls: list[Path] = []
        self.fail_on = fail_on or set()

    def transcribe(
        self, audio_path: str, *, offset: float | None = None, duration: float | None = None
    ) -> AsrResult:
        path = Path(audio_path)
        self.calls.append(path)
        if path.name in self.fail_on:
            raise RuntimeError("解码失败（测试注入）")
        return AsrResult(
            text=f"转写文本：{path.stem}",
            language="zh",
            segments=[AsrSegment(start=0.0, end=0.5, text=f"{path.stem} 你好")],
            audio_seconds=0.5,
            model="small",
        )


def make_factory(
    transcriber: FakeTranscriber,
    seen: list[dict[str, Any]] | None = None,
) -> Callable[..., FakeTranscriber]:
    def factory(**kwargs: Any) -> FakeTranscriber:
        if seen is not None:
            seen.append(kwargs)
        return transcriber

    return factory


def read_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_writes_txt_and_json_with_fake_transcriber(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = write_wav(tmp_path / "访谈01.wav", 0.5)
    out_dir = tmp_path / "out"
    fake = FakeTranscriber()
    seen: list[dict[str, Any]] = []

    code = g1_transcribe.main(
        ["--input", str(audio), "--out-dir", str(out_dir)],
        transcriber_factory=make_factory(fake, seen),
    )

    assert code == 0
    assert fake.calls == [audio]
    assert seen[0]["language"] == "zh"
    assert seen[0]["model_size"] == "small"

    txt_path = out_dir / "访谈01.txt"
    json_path = out_dir / "访谈01.json"
    assert txt_path.is_file()
    assert json_path.is_file()
    assert txt_path.read_text(encoding="utf-8") == "转写文本：访谈01\n"

    payload = read_json(json_path)
    assert set(payload) == {
        "text",
        "segments",
        "duration_seconds",
        "language",
        "model",
        "source",
    }
    assert payload["text"] == "转写文本：访谈01"
    assert payload["segments"] == [{"start": 0.0, "end": 0.5, "text": "访谈01 你好"}]
    assert payload["duration_seconds"] == 0.5
    assert payload["language"] == "zh"
    assert payload["model"] == "small"
    assert payload["source"] == str(audio)

    # 隐私口径：敏感信息 / 30 天 / 不联网上传，且不删除源音频。
    stdout = capsys.readouterr().out
    assert "敏感个人信息" in stdout
    assert "30 天内删除" in stdout
    assert "不联网上传" in stdout
    assert audio.is_file()


def test_name_conflict_gets_suffix(tmp_path: Path) -> None:
    audio = write_wav(tmp_path / "a.wav", 0.5)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    old_txt = out_dir / "a.txt"
    old_json = out_dir / "a.json"
    old_txt.write_text("旧转写", encoding="utf-8")
    old_json.write_text("{}", encoding="utf-8")

    code = g1_transcribe.main(
        ["--input", str(audio), "--out-dir", str(out_dir)],
        transcriber_factory=make_factory(FakeTranscriber()),
    )

    assert code == 0
    assert old_txt.read_text(encoding="utf-8") == "旧转写"
    assert old_json.read_text(encoding="utf-8") == "{}"
    assert (out_dir / "a_2.txt").is_file()
    assert (out_dir / "a_2.json").is_file()
    assert read_json(out_dir / "a_2.json")["text"] == "转写文本：a"


def test_directory_discovery_one_level_and_sorted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio_dir = tmp_path / "audio"
    nested = audio_dir / "nested"
    deep = nested / "deep"
    deep.mkdir(parents=True)
    write_wav(audio_dir / "b.wav", 0.5)
    write_wav(audio_dir / "a.wav", 0.5)
    write_wav(nested / "c.wav", 0.5)
    write_wav(deep / "d.wav", 0.5)
    (audio_dir / "notes.txt").write_text("不是音频", encoding="utf-8")

    out_dir = tmp_path / "out"
    code = g1_transcribe.main(
        ["--input", str(audio_dir), "--out-dir", str(out_dir), "--dry-run"]
    )

    assert code == 0
    assert not out_dir.exists()
    stdout = capsys.readouterr().out
    assert "a.wav" in stdout
    assert stdout.index("a.wav") < stdout.index("b.wav") < stdout.index("c.wav")
    assert "d.wav" not in stdout  # 只递归一层
    assert "notes.txt" not in stdout


def test_dry_run_does_not_build_transcriber(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = write_wav(tmp_path / "a.wav", 0.5)
    out_dir = tmp_path / "dry"

    def exploding_factory(**kwargs: Any) -> Any:
        raise AssertionError("dry-run 不应导入或构造 ASR")

    code = g1_transcribe.main(
        ["--input", str(audio), "--out-dir", str(out_dir), "--dry-run"],
        transcriber_factory=exploding_factory,
    )

    assert code == 0
    assert not out_dir.exists()
    stdout = capsys.readouterr().out
    assert "将转写" in stdout
    assert str(out_dir / "a.txt") in stdout
    assert str(out_dir / "a.json") in stdout


def test_overlong_wav_is_rejected_others_continue(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    short = write_wav(tmp_path / "short.wav", 0.05)
    long = write_wav(tmp_path / "long.wav", 7.0)
    out_dir = tmp_path / "out"
    fake = FakeTranscriber()

    code = g1_transcribe.main(
        [
            "--input",
            str(tmp_path),
            "--out-dir",
            str(out_dir),
            "--max-minutes",
            "0.1",  # 6 秒
        ],
        transcriber_factory=make_factory(fake),
    )

    assert code == 0
    assert fake.calls == [short]
    assert (out_dir / "short.txt").is_file()
    assert (out_dir / "short.json").is_file()
    assert not (out_dir / "long.txt").exists()
    assert not (out_dir / "long.json").exists()

    stdout = capsys.readouterr().out
    assert "long.wav" in stdout
    assert "上限" in stdout
    assert "跳过" in stdout


def test_non_wav_without_ffprobe_warns_and_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = tmp_path / "talk.mp3"
    audio.write_bytes(b"ID3\x04fake")
    out_dir = tmp_path / "out"
    monkeypatch.setattr(g1_transcribe.shutil, "which", lambda name: None)

    code = g1_transcribe.main(
        ["--input", str(audio), "--out-dir", str(out_dir)],
        transcriber_factory=make_factory(FakeTranscriber()),
    )

    assert code == 0
    assert (out_dir / "talk.txt").is_file()
    assert (out_dir / "talk.json").is_file()
    assert "ffprobe" in capsys.readouterr().out


def test_missing_dependency_reports_hint_and_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = write_wav(tmp_path / "a.wav", 0.5)
    out_dir = tmp_path / "out"

    def missing_factory(**kwargs: Any) -> Any:
        raise ImportError("No module named 'faster_whisper'")

    code = g1_transcribe.main(
        ["--input", str(audio), "--out-dir", str(out_dir)],
        transcriber_factory=missing_factory,
    )

    assert code == 2
    assert not (out_dir / "a.txt").exists()
    stderr = capsys.readouterr().err
    assert "faster_whisper" in stderr
    assert "uv sync --extra asr" in stderr
    assert "uv pip install faster-whisper" in stderr


def test_asr_unavailable_from_transcribe_also_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio = write_wav(tmp_path / "a.wav", 0.5)

    class UnavailableTranscriber:
        def transcribe(self, audio_path: str, **kwargs: Any) -> AsrResult:
            raise g1_transcribe.AsrUnavailable("ASR worker failed: 模型未就绪")

    code = g1_transcribe.main(
        ["--input", str(audio), "--out-dir", str(tmp_path / "out")],
        transcriber_factory=lambda **kwargs: UnavailableTranscriber(),
    )

    assert code == 2
    assert "uv sync --extra asr" in capsys.readouterr().err


def test_single_file_failure_returns_1_and_continues(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_wav(tmp_path / "a.wav", 0.5)
    write_wav(tmp_path / "b.wav", 0.5)
    out_dir = tmp_path / "out"
    fake = FakeTranscriber(fail_on={"a.wav"})

    code = g1_transcribe.main(
        ["--input", str(tmp_path), "--out-dir", str(out_dir)],
        transcriber_factory=make_factory(fake),
    )

    assert code == 1
    assert not (out_dir / "a.txt").exists()
    assert (out_dir / "b.txt").is_file()
    captured = capsys.readouterr()
    assert "[失败]" in captured.out
    assert "退出码 1" in captured.out


@pytest.mark.parametrize(
    "argv",
    [
        ["--input", "{missing}"],
        ["--input", "{txt}"],
        ["--input", "{wav}", "--max-minutes", "0"],
        ["--input", "{wav}", "--max-minutes", "-1"],
    ],
)
def test_argument_errors_exit_2(
    argv: list[str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    wav = write_wav(tmp_path / "a.wav", 0.5)
    txt = tmp_path / "note.txt"
    txt.write_text("x", encoding="utf-8")
    values = {"wav": str(wav), "txt": str(txt), "missing": str(tmp_path / "missing.wav")}
    resolved = [item.format(**values) for item in argv]

    with pytest.raises(SystemExit) as excinfo:
        g1_transcribe.main(resolved)

    assert excinfo.value.code == 2
    assert capsys.readouterr().err


def test_empty_directory_is_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    audio_dir = tmp_path / "empty"
    audio_dir.mkdir()
    out_dir = tmp_path / "out"

    code = g1_transcribe.main(["--input", str(audio_dir), "--out-dir", str(out_dir)])

    assert code == 0
    assert not out_dir.exists()
    assert "未发现支持的音频文件" in capsys.readouterr().out