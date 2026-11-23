"""分段延迟实测（F-005/F-013）：音频 -> ASR -> 意图 -> 门禁/下发 -> TTS。

方案把「说完→动作下发 P95 ≤ 3s」和「ASR RTF < 1」都写成**设计值**；本脚本用来
把它换成实测值，并且**分段**报告（模型加载 / 解码 / 意图 / 下发 / 合成），
因为总时长里最贵的一次性成本是模型加载，把它和稳态解码混在一个数字里没有意义。

    python scripts/measure_latency.py --audio clip.wav --duration 30
    python scripts/measure_latency.py --say "解锁入户门锁" --runs 5   # 只测决策链，不含 ASR
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from hvsl.app import build_engine  # noqa: E402
from hvsl.config import Settings  # noqa: E402
from hvsl.store import Store  # noqa: E402


def measure(args) -> dict:
    settings = Settings.load(args.env_file)
    engine = build_engine(settings.model_copy(update={"db_path": ":memory:"}), store=Store(":memory:"))
    rows: list[dict] = []

    asr = None
    if args.audio:
        from pipeline.asr import LocalAsr

        asr = LocalAsr(
            python_bin=settings.asr_python or None,
            model_dir=settings.asr_model_dir or None,
            model_size=settings.asr_model_size,
            device=args.device or settings.asr_device,
        )
    tts = None
    if args.with_tts:
        from pipeline.tts import Tts

        tts = Tts(out_path=settings.tts_wav_out)

    for index in range(args.runs):
        row: dict = {"run": index + 1}
        text = args.say
        if asr is not None and args.audio:
            result = asr.transcribe(args.audio, offset=args.offset, duration=args.duration)
            row["asr"] = {
                "audio_seconds": result.audio_seconds,
                "load_seconds": result.load_seconds,
                "decode_seconds": result.decode_seconds,
                "process_wall_seconds": result.wall_seconds,
                "decode_rtf": result.decode_rtf,
                "end_to_end_rtf": result.rtf,
                "windowed": result.windowed,
                "warnings": result.warnings,
                "device": result.device,
                "execution": result.execution,
                "segments": result.segment_count,
                "text": result.text[:80],
            }
            if not text:
                text = result.text
        if not text:
            raise SystemExit("没有可用于决策链的文本：请给 --audio 或 --say")

        decision = engine.handle(text)
        row["decision"] = {
            "kind": decision.kind.value,
            "risk": decision.risk_level.value,
            "dispatch": decision.dispatch_count,
            "timings_ms": decision.timings_ms,
        }
        if args.confirm_dangerous and decision.confirmation_token:
            confirmed = engine.confirm(decision.confirmation_token)
            row["confirm"] = {"kind": confirmed.kind.value, "timings_ms": confirmed.timings_ms}
        if tts is not None:
            synth = tts.synthesize(decision.message)
            row["tts"] = {"provider": synth.provider, "wall_seconds": synth.wall_seconds, "bytes": synth.audio_bytes}
        rows.append(row)
        print(f"[run {row['run']}] {json.dumps(row, ensure_ascii=False)}")

    summary = {
        "settings": {
            "audio": args.audio or "未使用（--say 直接给文本）",
            "runs": args.runs,
            "provider": settings.nlu_provider,
            "backend": engine.backend.kind,
            "entities": len(engine.backend.entities()),
        },
        "rows": rows,
        "aggregates": aggregates(rows),
        "口径声明": "本文件全部数字为**实测值**（模拟后端 + 规则解析；ASR 为本机 GPU）；"
        "接真实设备或云端 LLM 后需重新测量。字错率(CER)：**合成语音（已知文本）已实测**，"
        "见 `reports/ASR-CER.md`；**真人/家庭噪声无人工对齐参照，未实测**。",
    }
    engine.close()
    return summary


def aggregates(rows: list[dict]) -> dict:
    out: dict[str, dict[str, float]] = {}
    stage_paths = {
        "asr.load_seconds": ("asr", "load_seconds"),
        "asr.decode_seconds": ("asr", "decode_seconds"),
        "asr.end_to_end_seconds": ("asr", "process_wall_seconds"),
        "asr.decode_rtf": ("asr", "decode_rtf"),
        "nlu.ms": ("decision", "timings_ms", "nlu"),
        "dispatch.ms": ("decision", "timings_ms", "dispatch"),
        "decision_total.ms": ("decision", "timings_ms", "total"),
        "tts.seconds": ("tts", "wall_seconds"),
    }
    for label, path in stage_paths.items():
        values = []
        for row in rows:
            node = row
            for key in path:
                if not isinstance(node, dict) or key not in node:
                    node = None
                    break
                node = node[key]
            if isinstance(node, (int, float)):
                values.append(float(node))
        if values:
            out[label] = {
                "n": len(values),
                "mean": round(statistics.fmean(values), 3),
                "min": round(min(values), 3),
                "max": round(max(values), 3),
            }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="语音链路分段延迟实测")
    parser.add_argument("--audio", default="")
    parser.add_argument("--say", default="")
    parser.add_argument("--offset", type=float, default=None)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--device", default="", help="覆盖 ASR 设备（cuda/cpu）")
    parser.add_argument("--with-tts", action="store_true")
    parser.add_argument("--confirm-dangerous", action="store_true", help="危险指令自动确认一次以测量下发")
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--out", default=str(ROOT / "reports" / "语音链路分段实测.json"))
    args = parser.parse_args(argv)

    summary = measure(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n== 聚合 ==")
    print(json.dumps(summary["aggregates"], ensure_ascii=False, indent=2))
    print(f"\n写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
