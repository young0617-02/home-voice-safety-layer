"""Standalone faster-whisper worker: stdlib + faster_whisper only.

Launched either by :mod:`pipeline.asr` or directly with the ASR venv interpreter,
so it must not import the project package (that venv has no pydantic).

    <venv-python> pipeline/_asr_worker.py '{"audio":"a.wav","model_dir":"..."}'
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def fix_cuda_dll_path() -> list[str]:
    """Windows/ctranslate2 needs the nvidia cuBLAS DLLs discoverable before CUDA init."""
    added: list[str] = []
    if os.name != "nt":
        return added
    site = Path(sys.executable).parent.parent / "Lib" / "site-packages"
    for relative in (
        "nvidia/cublas/bin",
        "nvidia/cudnn/bin",
        "torch/lib",
        "ctranslate2",
    ):
        candidate = site / Path(*relative.split("/"))
        if candidate.is_dir():
            current = os.environ.get("PATH", "")
            if str(candidate) not in current:
                os.environ["PATH"] = str(candidate) + os.pathsep + current
                added.append(str(candidate))
    return added


def resolve_model_ref(model_size: str, model_dir: str | None) -> tuple[str, dict]:
    """Turn a configured model location into (model reference, WhisperModel kwargs).

    faster-whisper accepts either a local snapshot directory or a hub cache
    directory, and the operator usually points at whatever ``.hf-cache`` layout
    huggingface_hub produced. We look for ``model.bin`` at the given path and one
    cache hop below it, then fall back to treating it as a cache dir. Never
    reaches the network: local_files_only is always on when a directory is given.
    """
    if not model_dir:
        return model_size, {}
    root = Path(model_dir)
    candidates = [root]
    if not (root / "model.bin").is_file():
        # hub cache layout: <dir>/models--Systran--faster-whisper-<size>/snapshots/<hash>/model.bin
        for repo in sorted(root.glob("models--*")):
            if model_size.lower().replace("-", "") in repo.name.lower().replace("-", "").replace("systran", ""):
                candidates.extend(sorted((repo / "snapshots").glob("*"), reverse=True))
    for candidate in candidates:
        if (candidate / "model.bin").is_file():
            return str(candidate), {"local_files_only": True}
    return model_size, {"model_dir": str(root), "local_files_only": True}


def load_model(config: dict):
    from faster_whisper import WhisperModel

    model_ref, extra = resolve_model_ref(config.get("model_size", "large-v3"), config.get("model_dir"))
    errors: list[str] = []
    attempts = [
        (config.get("device", "cuda"), config.get("compute_type", "float16")),
        ("cpu", "int8"),
    ]
    for device, compute_type in attempts:
        kwargs = {"device": device, "compute_type": compute_type, **extra}
        try:
            model = WhisperModel(model_ref, **kwargs)
            return model, device, compute_type, errors
        except Exception as exc:  # CUDA init, missing DLL, OOM
            errors.append(f"{device}/{compute_type}: {type(exc).__name__}: {exc}")
    raise RuntimeError("cannot load faster-whisper model: " + " | ".join(errors))


def load_audio_window(config: dict):
    """Return (audio_input, window_seconds) for model.transcribe.

    faster-whisper 1.2.1's ``transcribe`` ignores nothing but ``decode_audio`` has no
    offset/duration arguments either - so a requested window is sliced here from the
    decoded sample array. Without slicing, "只转 20 秒" silently turned the whole
    81-minute recording over; that is how this bug was caught.
    """
    offset = config.get("offset")
    duration = config.get("duration")
    if offset is None and duration is None:
        return config["audio"], None
    try:
        try:
            from faster_whisper.audio import decode_audio
        except ImportError:
            from faster_whisper import decode_audio
        rate = int(config.get("sampling_rate", 16000))
        samples = decode_audio(config["audio"], sampling_rate=rate)
        start = int(float(offset or 0.0) * rate)
        stop = start + int(float(duration) * rate) if duration is not None else len(samples)
        window = samples[start:min(stop, len(samples))]
        if len(window) == 0:
            raise ValueError(f"窗口为空：offset={offset} duration={duration}")
        return window, float(len(window)) / rate
    except Exception as exc:
        config.setdefault("_warnings", []).append(f"窗口切片失败，退回整段音频：{type(exc).__name__}: {exc}")
        return config["audio"], None


def transcribe(config: dict) -> dict:
    fix_cuda_dll_path()
    load_started = time.perf_counter()
    model, device_used, compute_used, load_errors = load_model(config)
    load_seconds = round(time.perf_counter() - load_started, 3)

    audio_input, window_seconds = load_audio_window(config)
    kwargs = {
        "beam_size": int(config.get("beam_size", 1)),
        "vad_filter": bool(config.get("vad_filter", True)),
        "language": config.get("language"),
    }

    started = time.perf_counter()
    segment_iter, info = model.transcribe(audio_input, **kwargs)

    segments = []
    for segment in segment_iter:
        segments.append(
            {
                "start": round(float(segment.start), 3),
                "end": round(float(segment.end), 3),
                "text": (segment.text or "").strip(),
            }
        )
    wall = time.perf_counter() - started
    text = "".join(str(seg["text"]) for seg in segments)
    audio_seconds = window_seconds or float(getattr(info, "duration", 0.0) or 0.0)
    return {
        "text": text,
        "language": getattr(info, "language", "") or "",
        "segments": segments,
        "audio_seconds": round(audio_seconds, 3),
        "windowed": window_seconds is not None,
        "load_seconds": load_seconds,
        "decode_seconds": round(wall, 3),
        "model": config.get("model_size", "large-v3"),
        "device": device_used,
        "compute_type": compute_used,
        "load_warnings": load_errors,
        "warnings": config.get("_warnings", []),
    }


def main(argv: list[str]) -> int:
    try:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    except Exception:
        pass
    if len(argv) < 2:
        print(json.dumps({"error": "usage: _asr_worker.py '<json config>'"}), file=sys.stderr)
        return 2
    config = json.loads(argv[1])
    try:
        result = transcribe(config)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
