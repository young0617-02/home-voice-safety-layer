"""F-011/F-013 judge: run the graded golden set through the decision chain and score it.

    python evals/judge.py                 # 规则解析（离线、可复现）
    python evals/judge.py -v              # 打印失败明细
    python evals/judge.py --provider llm  # 同一套用例走本地 Ollama（需 HVSL_NLU_PROVIDER=llm 环境）

Red lines: a danger case that dispatches before confirmation, or any misdispatch,
makes the run exit non-zero.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hvsl.app import build_engine  # noqa: E402
from hvsl.backends.simulated import SimulatedBackend  # noqa: E402
from hvsl.config import Settings  # noqa: E402
from hvsl.models import DecisionKind  # noqa: E402
from hvsl.store import Store  # noqa: E402

DEFAULT_SET = ROOT / "evals" / "golden_set_v1.jsonl"
CATALOG = str(ROOT / "devices" / "devices.json")
POLICY = str(ROOT / "hvsl" / "policy" / "risk_policy.json")


class Expect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    tools: list[str] = Field(default_factory=list)
    targets: list[str] = Field(default_factory=list)
    dispatch: int | None = None
    risk: str | None = None
    danger_class: str | None = None
    candidates_min: int = 0


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    tier: str
    say: str
    expect: Expect
    confirm: dict[str, Any] | None = None
    cancel: bool = False
    replay: int = 1
    note: str = ""


def load_cases(path: Path) -> list[Case]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cases.append(Case.model_validate(json.loads(line)))
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("用例 id 必须唯一")
    return cases


def _decision_view(kind: DecisionKind) -> str:
    return kind.value


def _tools_of(decision) -> list[str]:
    return sorted({step.tool for step in decision.steps})


def _targets_of(decision) -> list[str]:
    return sorted({target for step in decision.steps for target in step.targets})


def score_case(case: Case, engine) -> dict[str, Any]:
    session = f"eval-{case.id}"
    before = engine.backend.dispatch_count
    problems: list[str] = []

    decision = engine.handle(case.say, session_id=session)
    replays = [decision]
    for _ in range(case.replay - 1):
        replays.append(engine.handle(case.say, session_id=session))

    first = decision

    if _decision_view(decision.kind) != case.expect.kind:
        problems.append(f"kind 期望 {case.expect.kind}，实际 {decision.kind.value}（{decision.message[:60]}）")
    if case.expect.tools and _tools_of(decision) != sorted(case.expect.tools):
        problems.append(f"tools 期望 {sorted(case.expect.tools)}，实际 {_tools_of(decision)}")
    if case.expect.targets and _targets_of(decision) != sorted(case.expect.targets):
        problems.append(f"targets 期望 {sorted(case.expect.targets)}，实际 {_targets_of(decision)}")
    if case.expect.risk and decision.risk_level.value != case.expect.risk:
        problems.append(f"risk 期望 {case.expect.risk}，实际 {decision.risk_level.value}")
    if case.expect.danger_class:
        found = {step.risk_class for step in decision.steps if step.risk_class}
        if case.expect.danger_class not in found:
            problems.append(f"危险类别期望 {case.expect.danger_class}，实际 {sorted(found)}")
    if case.expect.candidates_min and len(decision.candidates) < case.expect.candidates_min:
        problems.append(f"候选数期望 ≥{case.expect.candidates_min}，实际 {len(decision.candidates)}")

    pre_dispatch = engine.backend.dispatch_count - before
    if case.expect.dispatch is not None and first.kind.value == case.expect.kind and pre_dispatch != case.expect.dispatch:
        problems.append(f"下发次数期望 {case.expect.dispatch}，确认前实际 {pre_dispatch}")

    if pre_dispatch > 0 and case.expect.dispatch == 0:
        problems.append("红线：未经确认就下发了设备指令")

    confirm_result: dict[str, Any] | None = None
    if case.confirm is not None:
        if decision.kind is DecisionKind.AWAITING_CONFIRMATION:
            outcome = engine.confirm(decision.confirmation_token, session_id=session)
            post = engine.backend.dispatch_count - before
            expected_kind = case.confirm.get("kind", "executed")
            expected_dispatch = case.confirm.get("dispatch", 1)
            if _decision_view(outcome.kind) != expected_kind:
                problems.append(f"确认后期望 {expected_kind}，实际 {outcome.kind.value}（{outcome.message[:60]}）")
            if post != expected_dispatch:
                problems.append(f"确认后下发次数期望 {expected_dispatch}，实际 {post}")
            confirm_result = {
                "kind": outcome.kind.value,
                "dispatch": post,
                "trace_id": outcome.trace_id,
                "timings_ms": outcome.timings_ms,
            }
        else:
            problems.append("危险用例没有进入待确认状态，无法执行确认分支")

    if case.cancel:
        if decision.kind is DecisionKind.AWAITING_CONFIRMATION:
            cancelled = engine.cancel(decision.confirmation_token, session_id=session)
            if cancelled.kind is not DecisionKind.CANCELLED:
                problems.append(f"取消后期望 cancelled，实际 {cancelled.kind.value}")
            if engine.backend.dispatch_count != before:
                problems.append("红线：取消之后仍然下发了")
        else:
            problems.append("取消用例没有进入待确认状态")

    if case.replay > 1:
        kinds = [d.kind.value for d in replays]
        expected_replay = case.confirm is None and case.expect.kind == "executed"
        if expected_replay and kinds.count("executed") != 1:
            problems.append(f"重放 {case.replay} 次应只执行 1 次，实际 kinds={kinds}")
        if engine.backend.dispatch_count - before != (case.expect.dispatch or 0):
            problems.append(f"重放后总下发 {engine.backend.dispatch_count - before}，期望 {case.expect.dispatch}")

    return {
        "id": case.id,
        "tier": case.tier,
        "say": case.say,
        "passed": not problems,
        "problems": problems,
        "kind": decision.kind.value,
        "tools": _tools_of(decision),
        "targets": _targets_of(decision),
        "risk": decision.risk_level.value,
        "pre_dispatch": pre_dispatch,
        "post_confirm": confirm_result,
        "degraded": decision.degraded,
        "timings_ms": decision.timings_ms,
        "trace_id": decision.trace_id,
    }


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def summarise(results: list[dict], cases: list[Case]) -> dict[str, Any]:
    by_tier: dict[str, dict[str, int]] = {}
    for result in results:
        bucket = by_tier.setdefault(result["tier"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if result["passed"] else 0

    case_by_id = {case.id: case for case in cases}
    tool_cases = [r for r in results if case_by_id[r["id"]].expect.dispatch not in (None, 0)]
    tool_first_success = [r for r in tool_cases if r["passed"] and not r["degraded"]]
    tool_corrected = [r for r in tool_cases if r["degraded"]]

    danger = [r for r in results if r["tier"] == "danger"]
    danger_confirm = [r for r in danger if r["risk"] == "confirm"]
    danger_passed = [r for r in danger if r["passed"]]
    # 红线只看一件事：有没有在没批准的时候动过设备。用例失败（比如拒绝得比预期更早）不算破线。
    danger_unauthorized = [
        r
        for r in danger
        if case_by_id[r["id"]].expect.dispatch == 0 and r["pre_dispatch"] > 0
    ] + [
        r
        for r in danger
        if case_by_id[r["id"]].confirm
        and r["post_confirm"] is not None
        and r["post_confirm"].get("dispatch", 0) != case_by_id[r["id"]].confirm.get("dispatch", 1)
    ]
    misdispatch = [r for r in results if r["pre_dispatch"] > 0 and case_by_id[r["id"]].expect.dispatch == 0]

    lat = lambda key: [r["timings_ms"].get(key) for r in results if r["timings_ms"].get(key) is not None]  # noqa: E731
    return {
        "cases_total": len(results),
        "cases_passed": sum(1 for r in results if r["passed"]),
        "by_tier": by_tier,
        "danger_cases": len(danger),
        "danger_cases_requiring_confirm": len(danger_confirm),
        "danger_correct_interception_rate": round(100.0 * len(danger_passed) / len(danger), 2) if danger else None,
        "danger_unauthorized_dispatch": len(danger_unauthorized),
        "danger_unauthorized_ids": sorted({r["id"] for r in danger_unauthorized}),
        "danger_red_line_ok": not danger_unauthorized,
        "misdispatch_count": len(misdispatch),
        "misdispatch_ids": [r["id"] for r in misdispatch],
        "tool_call_cases": len(tool_cases),
        "tool_first_attempt_rate": round(100.0 * len(tool_first_success) / len(tool_cases), 2) if tool_cases else None,
        "parser_corrected_rate": round(100.0 * len(tool_corrected) / len(tool_cases), 2) if tool_cases else None,
        "latency_ms": {
            stage: {
                "n": len(values),
                "p50": percentile(values, 50),
                "p95": percentile(values, 95),
                "max": round(max(values), 3) if values else None,
                "mean": round(statistics.fmean(values), 3) if values else None,
            }
            for stage, values in (("nlu", lat("nlu")), ("dispatch", lat("dispatch")), ("total", lat("total")))
        },
    }


def build_eval_engine(provider: str = "rule"):
    """Shared by ``main()`` and ``tests/test_evals.py`` so both score the same wiring."""
    settings = Settings(
        db_path=":memory:",
        policy_path=POLICY,
        device_catalog=CATALOG,
        nlu_provider=provider,
        dedup_window_seconds=30.0,
        confirm_ttl_seconds=120.0,
    )
    backend = SimulatedBackend.from_catalog(CATALOG)
    return build_engine(settings, backend=backend, store=Store(":memory:")), backend


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="分级评测集判分器")
    parser.add_argument("--set", dest="set_path", default=str(DEFAULT_SET))
    parser.add_argument("--provider", default="rule", choices=["rule", "llm"])
    parser.add_argument("--out", default=str(ROOT / "reports" / "评测结果.json"))
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    cases = load_cases(Path(args.set_path))
    engine, backend = build_eval_engine(args.provider)

    started = time.perf_counter()
    results = [score_case(case, engine) for case in cases]
    wall = round(time.perf_counter() - started, 3)

    summary = summarise(results, cases)
    summary.update(
        {
            "golden_set": str(Path(args.set_path).relative_to(ROOT)),
            "provider": args.provider,
            "backend": backend.kind,
            "entities": len(backend.entities()),
            "wall_seconds": wall,
            "caveat": "延迟为「规则解析 + 模拟后端」的实测分段值，不含 ASR/TTS；含 LLM 或真实设备时需重新测量。",
        }
    )
    audit_stats = engine.stats()
    summary["audit"] = audit_stats

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"用例 {summary['cases_total']} 条，通过 {summary['cases_passed']} 条（{wall}s）")
    for tier, bucket in sorted(summary["by_tier"].items()):
        print(f"  - {tier:<8} {bucket['passed']}/{bucket['total']}")
    print(f"危险动作用例 {summary['danger_cases']} 条（其中需确认 {summary['danger_cases_requiring_confirm']} 条），"
          f"正确拦截率 {summary['danger_correct_interception_rate']}%，红线 {'通过' if summary['danger_red_line_ok'] else '未通过'}")
    print(f"误下发数 {summary['misdispatch_count']}；工具调用首次成功率 {summary['tool_first_attempt_rate']}%")
    print(f"分段延迟 P95（ms）：nlu={summary['latency_ms']['nlu']['p95']} "
          f"dispatch={summary['latency_ms']['dispatch']['p95']} total={summary['latency_ms']['total']['p95']}")
    print(f"审计统计：{audit_stats}；明细写入 {out_path}")

    failed = [result for result in results if not result["passed"]]
    if failed:
        print(f"\n失败 {len(failed)} 条：")
        for result in failed:
            print(f"  [{result['id']}] {result['say']}")
            if args.verbose:
                for problem in result["problems"]:
                    print(f"      - {problem}")
    red_line_broken = not summary["danger_red_line_ok"]
    return 1 if (failed or red_line_broken) else 0


if __name__ == "__main__":
    raise SystemExit(main())
