"""F-004 local ASR: faster-whisper with VAD segmentation, no wake word, no cloud.

The model is large and lives in its own venv, so this module runs it two ways:
in-process when ``faster_whisper`` is importable, otherwise as a subprocess
against the venv interpreter (``ASR_PYTHON_BIN``). Both paths are strictly local:
``local_files_only=True`` plus ``HF_HUB_OFFLINE=1`` so no audio and no model
lookup ever leaves the machine.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

WORKER = Path(__file__).with_name("_asr_worker.py")

#: R12：启动清扫只处理“自己创建”的临时音频，且必须超过 1 小时。
ASR_TEMP_PREFIX = "hvsl-"
ASR_TEMP_SUFFIXES = (".wav", ".pcm", ".tmp")
ORPHAN_TEMP_MAX_AGE_SECONDS = 3600.0
PROJECT_TMP_DIR = Path("var/tmp")


def _cleanup_orphan_temp_files(
    *,
    now: float | None = None,
    max_age_seconds: float = ORPHAN_TEMP_MAX_AGE_SECONDS,
    roots: list[Path] | None = None,
) -> list[Path]:
    """Delete only *our* stale temp audio; never touch a caller-provided recording.

    ASR itself does not write temp files today: :meth:`LocalAsr.transcribe` takes a
    path owned by the caller and never rewrites or deletes it.  This sweep is a
    defensive startup cleanup for future/NVMe-write failures: only ``hvsl-*``
    files with ``.wav/.pcm/.tmp`` and mtime older than ``max_age_seconds`` are
    removed.  Everything else (including any user audio path) is left untouched.
    """
    now = time.time() if now is None else now
    scan_roots = (
        [Path(root) for root in roots]
        if roots is not None
        else [PROJECT_TMP_DIR, Path(tempfile.gettempdir())]
    )
    removed: list[Path] = []
    for root in scan_roots:
        if not root.is_dir():
            continue
        for path in root.iterdir():
            if not path.is_file() or not path.name.startswith(ASR_TEMP_PREFIX):
                continue
            if path.suffix.lower() not in ASR_TEMP_SUFFIXES:
                continue
            try:
                if now - path.stat().st_mtime <= max_age_seconds:
                    continue
                path.unlink()
            except OSError:
                continue
            removed.append(path)
    return removed


class AsrSegment(BaseModel):
    start: float
    end: float
    text: str


class AsrResult(BaseModel):
    text: str
    language: str = ""
    segments: list[AsrSegment] = Field(default_factory=list)
    audio_seconds: float = 0.0
    load_seconds: float | None = None
    decode_seconds: float | None = None
    windowed: bool = False
    warnings: list[str] = Field(default_factory=list)
    wall_seconds: float = 0.0
    rtf: float | None = None
    decode_rtf: float | None = None
    model: str = ""
    device: str = ""
    execution: str = "in-process"

    @property
    def segment_count(self) -> int:
        return len(self.segments)


class FasterWhisperUnavailable(RuntimeError):
    pass


_OPENCC = None


def _to_simplified(text: str) -> str:
    """ASR 输出转简体（opencc 可选；未安装时原样返回）。"""
    global _OPENCC
    if not text:
        return text
    if _OPENCC is None:
        try:
            from opencc import OpenCC
        except ImportError:
            return text
        _OPENCC = OpenCC("t2s")
    return _OPENCC.convert(text)


def in_process_available() -> bool:
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return False
    return True


class LocalAsr:
    def __init__(
        self,
        *,
        python_bin: str | None = None,
        model_dir: str | None = None,
        model_size: str = "large-v3",
        device: str = "cuda",
        compute_type: str = "float16",
        beam_size: int = 1,
        vad_filter: bool = True,
        language: str = "zh",
        timeout_seconds: float = 1800.0,
    ) -> None:
        self.python_bin = python_bin or None
        self.model_dir = model_dir
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        self.vad_filter = vad_filter
        self.language = language or None
        self.timeout_seconds = timeout_seconds
        # R12：启动时清扫自己前缀的孤儿临时音频（>1h）；用户传入的文件永不删除。
        try:
            self.orphan_temp_files_removed = _cleanup_orphan_temp_files()
        except OSError:  # pragma: no cover - 清理失败绝不能阻止 ASR 启动
            self.orphan_temp_files_removed = []

    # ---------------------------------------------------------------- checks

    def availability(self) -> dict[str, Any]:
        """What F-001 needs: can we actually run ASR locally, and how?"""
        if in_process_available():
            return {"available": True, "execution": "in-process", "python": sys.executable}
        if self.python_bin and Path(self.python_bin).is_file():
            probe = subprocess.run(
                [str(self.python_bin), "-c", "import faster_whisper,sys;print(faster_whisper.__version__)"],
                capture_output=True,
                text=True,
                timeout=120,
            )
            return {
                "available": probe.returncode == 0,
                "execution": "subprocess",
                "python": str(self.python_bin),
                "version": probe.stdout.strip(),
                "error": probe.stderr.strip()[-400:] if probe.returncode else "",
            }
        return {
            "available": False,
            "execution": "none",
            "hint": "设置 ASR_PYTHON_BIN 指向装有 faster-whisper 的 venv，或 pip install faster-whisper",
        }

    def transcribe(
        self, audio_path: str | Path, *, offset: float | None = None, duration: float | None = None
    ) -> AsrResult:
        path = Path(audio_path)
        if not path.is_file():
            raise FileNotFoundError(f"audio file not found: {path}")
        # ASR 输入始终是调用方传入的文件：本模块不改写、不删除用户文件，也不创建
        # 临时音频；若未来引入临时文件，必须 try/finally: unlink（异常/超时同样清理）。
        config = {
            "audio": str(path),
            "model_dir": self.model_dir,
            "model_size": self.model_size,
            "device": self.device,
            "compute_type": self.compute_type,
            "beam_size": self.beam_size,
            "vad_filter": self.vad_filter,
            "language": self.language,
            "offset": offset,
            "duration": duration,
        }
        started = time.perf_counter()
        if in_process_available() and not self.python_bin:
            payload = self._run_in_process(config)
            execution = "in-process"
        else:
            payload = self._run_subprocess(config)
            execution = "subprocess"
        wall = time.perf_counter() - started
        payload.setdefault("segments", [])
        payload["text"] = _to_simplified(str(payload.get("text") or ""))
        for segment in payload["segments"]:
            if isinstance(segment, dict) and "text" in segment:
                segment["text"] = _to_simplified(str(segment["text"]))
        result = AsrResult(**payload)
        result.wall_seconds = round(wall, 3)
        result.execution = execution
        if result.audio_seconds > 0:
            result.rtf = round(wall / result.audio_seconds, 4)
            if result.decode_seconds:
                result.decode_rtf = round(result.decode_seconds / result.audio_seconds, 4)
        return result

    # ---------------------------------------------------------------- backends

    def _run_subprocess(self, config: dict[str, Any]) -> dict[str, Any]:
        python_bin = self.python_bin or sys.executable
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
        proc = subprocess.run(
            [str(python_bin), str(WORKER), json.dumps(config, ensure_ascii=False)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            timeout=self.timeout_seconds,
        )
        if proc.returncode != 0:
            raise FasterWhisperUnavailable(f"ASR worker failed: {proc.stderr.strip()[-800:]}")
        line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            raise FasterWhisperUnavailable(f"ASR worker returned non-JSON output: {proc.stdout[:400]}") from exc

    def _run_in_process(self, config: dict[str, Any]) -> dict[str, Any]:
        import importlib.util

        spec = importlib.util.spec_from_file_location("_asr_worker", WORKER)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise FasterWhisperUnavailable("cannot load ASR worker module")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.transcribe(config)
