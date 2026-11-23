"""Command line entry point.

    python -m hvsl.cli doctor         # F-001 environment gate
    python -m hvsl.cli chat           # text -> decision chain (no microphone needed)
    python -m hvsl.cli listen a.wav   # local ASR -> decision chain -> local TTS wav
    python -m hvsl.cli demo           # run demo/demo_script.json end to end
    python -m hvsl.cli audit --trace  # reverse lookup of one decision (F-009)
    python -m hvsl.cli tools          # namespaced tool list + JSON Schema
    python -m hvsl.cli serve          # loopback HTTP API
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .app import build_engine
from .config import Settings
from .models import DecisionKind
from .risk import RiskPolicy
from .tools import build_default_registry


def _settings(args: argparse.Namespace) -> Settings:
    return Settings.load(args.env_file) if getattr(args, "env_file", None) else Settings.load()


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _settings(args)
    engine = build_engine(settings)
    entities = engine.backend.entities()
    real = [e for e in entities if e.provenance.value == "real"]
    gate: dict[str, Any] = {
        "backend": engine.backend.kind,
        "entities_total": len(entities),
        "entities_real": len(real),
        "entities_simulated": len(entities) - len(real),
        "controllable_entity_ok": len(entities) > 0,
        "tools_registered": len(engine.registry.names()),
        "danger_classes": engine.policy.danger_classes(),
        "confirm_ttl_seconds": settings.confirm_ttl_seconds,
        "dedup_window_seconds": settings.dedup_window_seconds,
        "nlu_provider": settings.nlu_provider,
    }

    asr_info: dict[str, Any] = {"enabled": False}
    if args.with_asr:
        from pipeline.asr import LocalAsr

        asr_info = LocalAsr(
            python_bin=settings.asr_python or None,
            model_dir=settings.asr_model_dir or None,
            model_size=settings.asr_model_size,
            device=settings.asr_device,
        ).availability()
    gate["asr"] = asr_info

    tts_info: dict[str, Any] = {"enabled": False}
    if args.with_tts:
        from pipeline.tts import Tts

        tts = Tts(out_path=settings.tts_wav_out)
        tts_info = tts.availability()
        tts_info["enabled"] = True
    gate["tts"] = tts_info

    print(json.dumps(gate, ensure_ascii=False, indent=2))
    ok = gate["controllable_entity_ok"] and gate["tools_registered"] > 0
    if args.with_asr and not asr_info.get("available", True):
        print("警告：本地 ASR 不可用（F-004 未达标），决策链仍可用文本输入运行。", file=sys.stderr)
    engine.close()
    return 0 if ok else 1


def _print_decision(decision) -> None:
    icon = {
        DecisionKind.EXECUTED: "[已执行]",
        DecisionKind.AWAITING_CONFIRMATION: "[待确认]",
        DecisionKind.CLARIFICATION_REQUIRED: "[需澄清]",
        DecisionKind.REJECTED: "[已拒绝]",
        DecisionKind.DEDUPLICATED: "[已去重]",
        DecisionKind.CANCELLED: "[已取消]",
        DecisionKind.PARTIAL: "[部分执行]",
    }.get(decision.kind, "[结果]")
    print(f"{icon} {decision.message}")
    if decision.candidates:
        print("  候选：" + "、".join(decision.candidates))
    print(f"  trace={decision.trace_id} risk={decision.risk_level.value} dispatch={decision.dispatch_count}")


def cmd_chat(args: argparse.Namespace) -> int:
    settings = _settings(args)
    engine = build_engine(settings)
    print(f"决策层已就绪（后端={engine.backend.kind}，实体={len(engine.backend.entities())} 个）。输入 /quit 退出。")
    while True:
        try:
            text = input("你说> ").strip()
        except EOFError:
            break
        if not text:
            continue
        if text in {"/quit", "/exit"}:
            break
        if text in {"/help", "/?"}:
            print("内置命令：/pending /confirm <token> /cancel <token> /audit [trace] /help /quit")
            continue
        if text == "/pending":
            for pending in engine.pending():
                print(f"  token={pending.token} 待确认：{pending.summary()}")
            continue
        if text.startswith("/confirm"):
            token = text.split()[1] if len(text.split()) > 1 else None
            _print_decision(engine.confirm(token))
            continue
        if text.startswith("/cancel"):
            token = text.split()[1] if len(text.split()) > 1 else None
            _print_decision(engine.cancel(token))
            continue
        if text.startswith("/audit"):
            trace = text.split()[1] if len(text.split()) > 1 else None
            _print_trace(engine, trace, limit=args.limit)
            continue
        _print_decision(engine.handle(text))
    engine.close()
    return 0


def cmd_listen(args: argparse.Namespace) -> int:
    from pipeline.asr import LocalAsr
    from pipeline.tts import Tts

    settings = _settings(args)
    engine = build_engine(settings)
    asr = LocalAsr(
        python_bin=settings.asr_python or None,
        model_dir=settings.asr_model_dir or None,
        model_size=settings.asr_model_size,
        device=settings.asr_device,
    )
    result = asr.transcribe(args.audio, offset=args.offset, duration=args.duration)
    print(f"[ASR {result.execution}/{result.device}] {result.text}")
    print(
        f"  音频 {result.audio_seconds}s / 耗时 {result.wall_seconds}s / RTF {result.rtf} / 分段 {result.segment_count}"
    )
    decision = engine.handle(result.text)
    _print_decision(decision)
    if not args.no_tts:
        tts = Tts(out_path=settings.tts_wav_out)
        synth = tts.synthesize(decision.message)
        print(f"[TTS {synth.provider}] {synth.wall_seconds}s -> {synth.audio_path or synth.note}")
    engine.close()
    return 0


def cmd_say(args: argparse.Namespace) -> int:
    from pipeline.tts import Tts

    settings = _settings(args)
    tts = Tts(out_path=settings.tts_wav_out, speak_out_loud=args.loud)
    result = tts.synthesize(args.text)
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    settings = _settings(args)
    engine = build_engine(settings)
    script = json.loads(Path(args.script).read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    failures = 0
    print(f"== {script['title']} ==")
    for case in script["steps"]:
        decision = engine.handle(case["say"], session_id=case.get("session_id", "demo"))
        parked_kind = decision.kind.value
        passed = True
        if case.get("expect_parked"):
            passed = parked_kind == case["expect_parked"]
        if case.get("confirm"):
            if decision.confirmation_token:
                decision = engine.confirm(decision.confirmation_token, session_id=case.get("session_id", "demo"))
        expected = case["expect_kind"]
        passed = passed and decision.kind.value == expected
        if case.get("max_dispatch") is not None:
            passed = passed and decision.dispatch_count <= case["max_dispatch"]
        if not passed:
            failures += 1
        _print_decision(decision)
        rows.append(
            {
                "say": case["say"],
                "confirm": bool(case.get("confirm")),
                "parked_kind": parked_kind,
                "expected": expected,
                "expected_parked": case.get("expect_parked"),
                "actual": decision.kind.value,
                "dispatch": decision.dispatch_count,
                "risk": decision.risk_level.value,
                "trace_id": decision.trace_id,
                "timings_ms": decision.timings_ms,
                "passed": passed,
            }
        )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"title": script["title"], "steps": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n演示步骤 {len(rows)} 条，失败 {failures} 条；明细写入 {out}")
    engine.close()
    return 0 if failures == 0 else 1


def _print_trace(engine, trace_id: str | None, *, limit: int = 20) -> None:
    records = engine.trace(trace_id) if trace_id else engine.audit.query(limit=limit)
    if not records:
        print("  （没有审计记录）")
        return
    for record in records:
        print(
            f"  [{record.stage:<9}] risk={record.risk_level or '-':<7} confirm={record.confirmation_state or '-':<13}"
            f" dispatch={record.dispatch_state or '-':<12} tool={record.tool or '-'} targets={record.targets}"
            + (f" error={record.error}" if record.error else "")
        )


def cmd_audit(args: argparse.Namespace) -> int:
    engine = build_engine(_settings(args))
    _print_trace(engine, args.trace, limit=args.limit)
    print(f"统计：{engine.stats()}")
    engine.close()
    return 0


def cmd_tools(args: argparse.Namespace) -> int:
    registry = build_default_registry()
    policy = RiskPolicy.load(_settings(args).policy_path)
    payload = {
        "namespace": registry.namespace,
        "tools": registry.json_schemas(),
        "danger_classes": policy.danger_classes(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server

    run_server(_settings(args))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hvsl", description="本地优先智能家居语音助手：决策与安全层")
    parser.add_argument("--env-file", dest="env_file", default=None, help="可选的 .env 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_doctor = sub.add_parser("doctor", help="环境闸门自检（F-001）")
    p_doctor.add_argument("--with-asr", action="store_true", help="同时探测 faster-whisper")
    p_doctor.add_argument("--with-tts", action="store_true", help="同时探测本地 TTS")
    p_doctor.set_defaults(func=cmd_doctor)

    p_chat = sub.add_parser("chat", help="文本输入跑完整决策链")
    p_chat.add_argument("--limit", type=int, default=20)
    p_chat.set_defaults(func=cmd_chat)

    p_listen = sub.add_parser("listen", help="音频文件 -> 本地 ASR -> 决策链 -> 本地 TTS")
    p_listen.add_argument("audio")
    p_listen.add_argument("--offset", type=float, default=None)
    p_listen.add_argument("--duration", type=float, default=None)
    p_listen.add_argument("--no-tts", action="store_true")
    p_listen.set_defaults(func=cmd_listen)

    p_say = sub.add_parser("say", help="本地 TTS 合成一句应答到 WAV")
    p_say.add_argument("text")
    p_say.add_argument("--loud", action="store_true", help="用默认扬声器播放（会出声，需显式开启）")
    p_say.set_defaults(func=cmd_say)

    p_demo = sub.add_parser("demo", help="盲跑演示脚本（A11）")
    p_demo.add_argument("--script", default="demo/demo_script.json")
    p_demo.add_argument("--out", default="var/demo_run.json")
    p_demo.set_defaults(func=cmd_demo)

    p_audit = sub.add_parser("audit", help="反查留痕（F-009）")
    p_audit.add_argument("--trace", default=None)
    p_audit.add_argument("--limit", type=int, default=20)
    p_audit.set_defaults(func=cmd_audit)

    p_tools = sub.add_parser("tools", help="打印带命名空间的工具表与 JSON Schema")
    p_tools.set_defaults(func=cmd_tools)

    p_serve = sub.add_parser("serve", help="启动仅监听本机的 HTTP 接口")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
