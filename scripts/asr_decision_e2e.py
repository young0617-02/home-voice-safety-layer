"""真实语音链路：TTS 生成的 WAV -> faster-whisper small -> 决策引擎。

前置：`var/asr/kai_da_men.wav` 与 `var/asr/kai_ke_ting_da_deng.wav` 已由
Windows SAPI（Microsoft Huihui, zh-CN）生成；本脚本用真实 ASR 模型转写，
再把文本喂给 `hvsl.app.build_engine`（simulated backend），验证决策语义。

边界：合成语音、近场、无噪声；不是真人/远场/方言证据。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hvsl.app import build_engine  # noqa: E402
from hvsl.backends.simulated import SimulatedBackend  # noqa: E402
from hvsl.config import Settings  # noqa: E402
from pipeline.asr import LocalAsr  # noqa: E402

CASES = (
    {
        "wav": ROOT / "var" / "asr" / "kai_da_men.wav",
        "expect_kind": "awaiting_confirmation",
        "expect_dispatch": 0,
        "label": "危险：开大门",
    },
    {
        "wav": ROOT / "var" / "asr" / "kai_ke_ting_da_deng.wav",
        "expect_kind": "executed",
        "expect_dispatch": 1,
        "label": "低风险：打开客厅大灯",
    },
)


def main() -> int:
    out_dir = ROOT / "var" / "asr"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, object]] = []

    asr = LocalAsr(model_size="small", device="cpu", language="zh")
    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings(
            db_path=str(Path(tmp) / "asr-e2e.db"),
            policy_path=str(ROOT / "hvsl" / "policy" / "risk_policy.json"),
            device_catalog=str(ROOT / "devices" / "devices.json"),
            nlu_provider="rule",
        )
        backend = SimulatedBackend.from_catalog(settings.device_catalog)
        engine = build_engine(settings, backend=backend)
        try:
            for case in CASES:
                wav = Path(case["wav"])
                asr_result = asr.transcribe(str(wav))
                text = str(getattr(asr_result, "text", asr_result)).strip()
                decision = engine.handle(text, session_id="asr-e2e")
                ok = (
                    decision.kind.value == case["expect_kind"]
                    and decision.dispatch_count == case["expect_dispatch"]
                )
                item = {
                    "label": case["label"],
                    "wav": str(wav.relative_to(ROOT)),
                    "asr_text": text,
                    "kind": decision.kind.value,
                    "dispatch_count": decision.dispatch_count,
                    "expected": {
                        "kind": case["expect_kind"],
                        "dispatch_count": case["expect_dispatch"],
                    },
                    "verdict": "pass" if ok else "fail",
                    "message": decision.message,
                }
                results.append(item)
                print(
                    f"[{'PASS' if ok else 'FAIL'}] {case['label']}: ASR={text!r} "
                    f"kind={item['kind']} dispatch={item['dispatch_count']}"
                )
        finally:
            engine.close()

    (out_dir / "e2e_results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    passed = sum(1 for item in results if item["verdict"] == "pass")
    print(f"\n语音链路：{passed}/{len(results)} PASS；明细 {out_dir / 'e2e_results.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())