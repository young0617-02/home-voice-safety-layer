"""Slice a short WAV clip out of a long recording, for realistic end-to-end timing.

必须用装有 faster-whisper 的 venv 跑（它带 PyAV）：

    <venv-python> scripts/make_clip.py --audio 长录音.m4a --offset 120 --duration 20 --out var/clip.wav
"""

from __future__ import annotations

import argparse
import array
import sys
import wave
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从长音频切出短片段（WAV，16k 单声道）")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--out", required=True)
    parser.add_argument("--rate", type=int, default=16000)
    args = parser.parse_args(argv)

    try:
        from faster_whisper.audio import decode_audio
    except ImportError:
        from faster_whisper import decode_audio  # type: ignore[no-redef]

    samples = decode_audio(args.audio, sampling_rate=args.rate)
    start = int(args.offset * args.rate)
    stop = min(len(samples), start + int(args.duration * args.rate))
    window = samples[start:stop]
    if len(window) == 0:
        print(f"窗口为空：offset={args.offset} duration={args.duration}", file=sys.stderr)
        return 2

    pcm = array.array(
        "h",
        [int(max(-32767, min(32767, round(float(sample) * 32767)))) for sample in window],
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(out), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(args.rate)
        handle.writeframes(pcm.tobytes())

    print(f"已写出 {out}：{len(window) / args.rate:.2f}s @ {args.rate}Hz（源 {args.audio}，offset {args.offset}s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
