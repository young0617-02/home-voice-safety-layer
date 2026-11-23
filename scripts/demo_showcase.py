"""5 分钟演示：一条命令跑完安全决策链的 5 个关键行为。

用法（命令与参数以 ``--help`` 为准）：
    # 完整演示（临时 SQLite，演示结束自动清理）
    python scripts/demo_showcase.py

    # 只打印 5 步计划，不建引擎、不写数据库/审计文件
    python scripts/demo_showcase.py --dry-run

    # 机器可读摘要（不含确认令牌）
    python scripts/demo_showcase.py --json

    # 把审计 CSV/JSON 留在一个可见目录（默认写临时目录并随退出删除）
    python scripts/demo_showcase.py --audit-dir demo_out

5 步：
    1. 低风险「开客厅大灯」-> executed，dispatch_count=1
    2. 危险「开大门」-> awaiting_confirmation，dispatch_count=0，回复里没有令牌
    3. 用第 2 步的令牌确认 -> executed，dispatch_count=1
    4. 同一令牌再确认 -> rejected/deduplicated，不得再次下发
    5. 审计导出 CSV/JSON -> 记录条数、路径、3 条关键决策

复用关系：
    引擎由 ``hvsl.app.build_engine`` 装配，后端固定为 ``SimulatedBackend``；
    决策、风险分级、确认门禁、幂等与审计全部走真实实现，本脚本只编排与断言，
    不复制任何判定逻辑。

边界：
    这是 simulated 后端的确定性演示，不是 Home Assistant 真机证据；
    不接真实 HA、不联网、不启用 ASR/TTS。确认令牌只在进程内存中使用，
    不进入 stdout / JSON / 审计文件。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hvsl.app import build_engine  # noqa: E402
from hvsl.audit import export_records  # noqa: E402
from hvsl.backends.simulated import SimulatedBackend  # noqa: E402
from hvsl.config import AuditRecord, Settings  # noqa: E402
from hvsl.models import Decision, DecisionKind  # noqa: E402

DEMO_NAME = "home-voice-safety-layer/showcase"
SESSION_ID = "demo-showcase"
BOUNDARY = "模拟后端演示（simulated backend），不是 Home Assistant 真机证据"

#: 第 1 步必须落在唯一可解析的实体上：catalog 里客厅有两盏灯，
#: 「开客厅灯」会被引擎判为 clarification_required（这是安全行为，不猜），
#: 所以演示用「开客厅大灯」，详见 docs/演示脚本-v0.1.md 的口径差异。
LOW_RISK_UTTERANCE = "开客厅大灯"
DANGER_UTTERANCE = "开大门"

PLAN: tuple[dict[str, Any], ...] = (
    {
        "index": 1,
        "title": "低风险直执行",
        "utterance": LOW_RISK_UTTERANCE,
        "expect": "executed，dispatch_count=1",
        "narration": "普通开灯不值得打断用户：它应该直接执行，且只下发一次。",
    },
    {
        "index": 2,
        "title": "危险动作挂起",
        "utterance": DANGER_UTTERANCE,
        "expect": "awaiting_confirmation，dispatch_count=0，回复不含令牌",
        "narration": "注意看：它现在不会开门。危险动作先挂起，下发次数必须是 0。",
    },
    {
        "index": 3,
        "title": "确认后执行",
        "utterance": "（用第 2 步返回的令牌调用 confirm）",
        "expect": "executed，dispatch_count=1",
        "narration": "我现在才确认。注意下发次数从 0 变成 1，而且只执行一次。",
    },
    {
        "index": 4,
        "title": "重复不重发",
        "utterance": "（同一令牌再确认一次）",
        "expect": "deduplicated 或 rejected，dispatch_count=0",
        "narration": "同一张确认票再用一次——它必须拒绝或去重，绝不能再次开门。",
    },
    {
        "index": 5,
        "title": "审计可导出",
        "utterance": "（导出 audit.csv / audit.json）",
        "expect": "包含 3 条关键决策，令牌不落盘",
        "narration": "最后看审计：原句、风险、是否确认、是否下发，都能导出核对。",
    },
)


@dataclass(frozen=True)
class StepRecord:
    """一步的实际结果 + 预期校验；``failures`` 为空才算通过。"""

    index: int
    title: str
    narration: str
    utterance: str
    kind: str
    dispatch_count: int
    spoken_message: str = ""
    message: str = ""
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    detail_lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    metric_label: str = "下发次数"

    @property
    def passed(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "utterance": self.utterance,
            "kind": self.kind,
            "dispatch_count": self.dispatch_count,
            "spoken_message": self.spoken_message,
            "message": self.message,
            "outcomes": self.outcomes,
            "detail": self.detail_lines,
            "passed": self.passed,
            "failures": self.failures,
        }


@dataclass
class DemoResult:
    summary: dict[str, Any]
    #: 仅供进程内校验“令牌没有泄露”使用：不写日志、不序列化、repr 也不显示。
    confirmation_token: str = field(default="", repr=False)
    export_dir: Path | None = None


def run_demo(*, temp_root: Path | None = None, audit_dir: Path | None = None) -> DemoResult:
    """跑完 5 步并返回摘要；临时目录在返回前即被清理。

    ``temp_root`` 仅用于测试，决定临时目录的父目录；``audit_dir`` 给定时
    审计文件写在该目录（保留），否则写临时目录（随演示退出删除）。
    """
    steps: list[StepRecord] = []
    token = ""

    with tempfile.TemporaryDirectory(
        prefix="hvsl-showcase-", dir=str(temp_root) if temp_root is not None else None
    ) as tmp:
        workdir = Path(tmp)
        settings = Settings(
            db_path=str(workdir / "demo.db"),
            policy_path=str(ROOT / "hvsl" / "policy" / "risk_policy.json"),
            device_catalog=str(ROOT / "devices" / "devices.json"),
            nlu_provider="rule",
        )
        # 显式注入 simulated 后端：即使环境里配了 HA_BASE_URL 也不会碰真实设备。
        backend = SimulatedBackend.from_catalog(settings.device_catalog)
        engine = build_engine(settings, backend=backend)
        try:
            low_risk = engine.handle(LOW_RISK_UTTERANCE, session_id=SESSION_ID)
            steps.append(_check_low_risk(low_risk, backend))

            danger = engine.handle(DANGER_UTTERANCE, session_id=SESSION_ID)
            token = danger.confirmation_token or ""
            steps.append(_check_danger(danger, token, backend))

            confirmed = (
                engine.confirm(token=token, session_id=SESSION_ID, user_words="确认") if token else None
            )
            steps.append(_check_confirm(confirmed, backend))

            replay = (
                engine.confirm(token=token, session_id=SESSION_ID, user_words="确认") if token else None
            )
            steps.append(_check_replay(replay, backend))

            audit_rows = engine.audit.query_for_export(limit=100_000)
        finally:
            engine.close()

        export_dir = audit_dir if audit_dir is not None else workdir / "audit"
        csv_path = export_dir / "audit.csv"
        json_path = export_dir / "audit.json"
        csv_rows = export_records(audit_rows, csv_path, fmt="csv")
        json_rows = export_records(audit_rows, json_path, fmt="json")
        key_decisions = _count_key_decisions(audit_rows)
        token_leaked = _exports_contain_token(csv_path, json_path, token)
        steps.append(
            _check_audit(
                rows=audit_rows,
                csv_rows=csv_rows,
                json_rows=json_rows,
                key_decisions=key_decisions,
                csv_path=csv_path,
                json_path=json_path,
                token_leaked=token_leaked,
            )
        )

        passed = all(step.passed for step in steps)
        summary: dict[str, Any] = {
            "demo": DEMO_NAME,
            "mode": "live",
            "backend": "simulated",
            "boundary": BOUNDARY,
            "session_id": SESSION_ID,
            "steps": [step.to_dict() for step in steps],
            "audit": {
                "record_count": csv_rows,
                "json_record_count": json_rows,
                "key_decisions": key_decisions,
                "csv_path": str(csv_path),
                "json_path": str(json_path),
                "token_leaked": token_leaked,
                "retained": audit_dir is not None,
            },
            "backend_dispatches": backend.dispatch_count,
            "passed": passed,
        }
        return DemoResult(summary=summary, confirmation_token=token, export_dir=export_dir)


def _outcome_rows(decision: Decision) -> list[dict[str, Any]]:
    """脱敏后的关键结果：不含 idempotency_key、不含任何令牌。"""
    return [
        {
            "tool": outcome.tool,
            "targets": list(outcome.targets),
            "state": outcome.state,
            "dispatched": outcome.dispatched,
        }
        for outcome in decision.outcomes
    ]


def _base_record(
    *,
    index: int,
    decision: Decision | None,
    narration: str,
    utterance: str,
    failures: list[str],
    utterance_override: str | None = None,
) -> StepRecord:
    plan = PLAN[index - 1]
    if utterance_override is not None:
        shown_utterance = utterance_override
    elif decision is None:
        shown_utterance = utterance
    else:
        shown_utterance = decision.utterance or utterance
    return StepRecord(
        index=index,
        title=str(plan["title"]),
        narration=narration,
        utterance=shown_utterance,
        kind="not_executed" if decision is None else decision.kind.value,
        dispatch_count=0 if decision is None else decision.dispatch_count,
        spoken_message="" if decision is None else decision.spoken_message,
        message="" if decision is None else decision.message,
        outcomes=[] if decision is None else _outcome_rows(decision),
        failures=failures,
    )


def _check_low_risk(decision: Decision, backend: SimulatedBackend) -> StepRecord:
    failures: list[str] = []
    if decision.kind is not DecisionKind.EXECUTED:
        failures.append(f"预期 executed，实际 {decision.kind.value}")
    if decision.dispatch_count != 1:
        failures.append(f"预期 dispatch_count=1，实际 {decision.dispatch_count}")
    if backend.dispatch_count != 1:
        failures.append(f"预期后端收到 1 次下发，实际 {backend.dispatch_count}")
    return _base_record(
        index=1,
        decision=decision,
        narration=PLAN[0]["narration"],
        utterance=LOW_RISK_UTTERANCE,
        failures=failures,
    )


def _check_danger(decision: Decision, token: str, backend: SimulatedBackend) -> StepRecord:
    failures: list[str] = []
    if decision.kind is not DecisionKind.AWAITING_CONFIRMATION:
        failures.append(f"预期 awaiting_confirmation，实际 {decision.kind.value}")
    if decision.dispatch_count != 0:
        failures.append(f"预期 dispatch_count=0，实际 {decision.dispatch_count}")
    if backend.dispatch_count != 1:
        failures.append(f"预期后端仍只有第 1 步的 1 次下发，实际 {backend.dispatch_count}")
    if not token:
        failures.append("预期返回确认令牌，实际为空")
    if token and token in decision.spoken_message:
        failures.append("spoken_message 泄露了确认令牌")
    if token and token in decision.message:
        failures.append("message 泄露了确认令牌")
    return _base_record(
        index=2,
        decision=decision,
        narration=PLAN[1]["narration"],
        utterance=DANGER_UTTERANCE,
        failures=failures,
    )


def _check_confirm(decision: Decision | None, backend: SimulatedBackend) -> StepRecord:
    failures: list[str] = []
    if decision is None:
        failures.append("没有拿到确认令牌，无法执行确认")
    else:
        if decision.kind is not DecisionKind.EXECUTED:
            failures.append(f"预期 executed，实际 {decision.kind.value}")
        if decision.dispatch_count != 1:
            failures.append(f"预期 dispatch_count=1，实际 {decision.dispatch_count}")
        if backend.dispatch_count != 2:
            failures.append(f"预期后端累计 2 次下发，实际 {backend.dispatch_count}")
    record = _base_record(
        index=3,
        decision=decision,
        narration=PLAN[2]["narration"],
        utterance="（用第 2 步返回的令牌调用 confirm）",
        failures=failures,
        utterance_override="确认（confirm）",
    )
    return record


def _check_replay(decision: Decision | None, backend: SimulatedBackend) -> StepRecord:
    failures: list[str] = []
    allowed = {DecisionKind.DEDUPLICATED, DecisionKind.REJECTED}
    if decision is None:
        failures.append("没有拿到确认令牌，无法验证重复确认")
    else:
        if decision.kind not in allowed:
            failures.append(f"预期 deduplicated 或 rejected，实际 {decision.kind.value}")
        if decision.dispatch_count != 0:
            failures.append(f"预期 dispatch_count=0，实际 {decision.dispatch_count}")
        if any(outcome.dispatched for outcome in decision.outcomes):
            failures.append("重复确认仍然发生了下发")
        if backend.dispatch_count != 2:
            failures.append(f"预期后端累计仍为 2 次下发，实际 {backend.dispatch_count}")
    return _base_record(
        index=4,
        decision=decision,
        narration=PLAN[3]["narration"],
        utterance="（同一令牌再确认一次）",
        failures=failures,
        utterance_override="确认（重复，同一令牌）",
    )


def _count_key_decisions(rows: list[AuditRecord]) -> int:
    """3 条关键决策：两次真实下发（灯/门）+ 一次危险动作挂起。"""
    executed = sum(1 for row in rows if row.stage == "dispatch" and row.dispatch_state == "executed")
    parked = sum(1 for row in rows if row.stage == "confirm" and row.confirmation_state == "pending")
    return executed + parked


def _exports_contain_token(csv_path: Path, json_path: Path, token: str) -> bool:
    if not token:
        return False
    for path in (csv_path, json_path):
        try:
            if token in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            return True
    return False


def _check_audit(
    *,
    rows: list[AuditRecord],
    csv_rows: int,
    json_rows: int,
    key_decisions: int,
    csv_path: Path,
    json_path: Path,
    token_leaked: bool,
) -> StepRecord:
    failures: list[str] = []
    if not rows:
        failures.append("审计表为空")
    if csv_rows != len(rows):
        failures.append(f"CSV 行数与审计记录不一致：{csv_rows} != {len(rows)}")
    if json_rows != len(rows):
        failures.append(f"JSON 行数与审计记录不一致：{json_rows} != {len(rows)}")
    if key_decisions != 3:
        failures.append(f"预期 3 条关键决策，实际 {key_decisions}")
    for path in (csv_path, json_path):
        if not path.is_file() or path.stat().st_size == 0:
            failures.append(f"审计文件缺失或为空：{path}")
    if token_leaked:
        failures.append("审计导出中出现了确认令牌")

    detail_lines = [
        f"审计记录：{len(rows)} 行（CSV {csv_rows} 行 / JSON {json_rows} 条）",
        f"关键决策：{key_decisions} 条（2 条已执行下发 + 1 条待确认挂起）",
        f"CSV 路径：{csv_path}",
        f"JSON 路径：{json_path}",
        "原句默认脱敏：utterance 列写为 [redacted]，令牌从不落盘",
    ]
    return StepRecord(
        index=5,
        title=str(PLAN[4]["title"]),
        narration=str(PLAN[4]["narration"]),
        utterance="（导出 audit.csv / audit.json）",
        kind="audit_export",
        dispatch_count=csv_rows,
        detail_lines=detail_lines,
        failures=failures,
        metric_label="记录条数",
    )


# --------------------------------------------------------------------- 输出


def _print_header() -> None:
    print("=" * 64)
    print("家庭语音安全层 · 5 分钟演示")
    print("后端：simulated（模拟实体，不接真实负载）")
    print(f"会话：{SESSION_ID}")
    print(f"边界：{BOUNDARY}")
    print("=" * 64)
    print()


def _print_step(step: StepRecord) -> None:
    print(f"[{step.index}/5] {step.title}")
    print(f"  演示台词：{step.narration}")
    print(f"  用户说：{step.utterance}")
    print(f"  决策：{step.kind}")
    print(f"  {step.metric_label}：{step.dispatch_count}")
    if step.spoken_message:
        print(f"  系统回复（TTS 文本）：{step.spoken_message}")
    for outcome in step.outcomes:
        targets = "、".join(outcome["targets"])
        dispatched = "已下发" if outcome["dispatched"] else "未下发"
        print(f"  关键结果：{outcome['tool']} -> {targets}：{outcome['state']}（{dispatched}）")
    for line in step.detail_lines:
        print(f"  {line}")
    if step.passed:
        print("  校验：[OK]")
    else:
        print(f"  校验：[FAIL] {'；'.join(step.failures)}")
    print()


def _print_live(result: DemoResult) -> None:
    _print_header()
    for step in result.summary["steps"]:
        _print_step(
            StepRecord(
                index=step["index"],
                title=step["title"],
                narration=PLAN[step["index"] - 1]["narration"],
                utterance=step["utterance"],
                kind=step["kind"],
                dispatch_count=step["dispatch_count"],
                spoken_message=step["spoken_message"],
                message=step["message"],
                outcomes=step["outcomes"],
                detail_lines=step["detail"],
                failures=step["failures"],
                metric_label="记录条数" if step["index"] == 5 else "下发次数",
            )
        )
    if result.summary["passed"]:
        print("结果：5/5 全部通过")
    else:
        print("结果：存在不符合预期的步骤，见上方 [FAIL]")
    if result.summary["audit"]["retained"]:
        print(f"审计已保留在：{result.export_dir}")
    else:
        print("审计目录为临时目录，随演示退出删除；如需留存请加 --audit-dir PATH。")
    print()
    print(f"边界重申：{BOUNDARY}；不构成真机验证。")


def _print_dry_run() -> None:
    print("5 分钟演示计划（dry-run：不建引擎、不写 SQLite、不导审计）")
    print(f"边界：{BOUNDARY}")
    print()
    for step in PLAN:
        print(f"[{step['index']}/5] {step['title']}")
        print(f"  用户说：{step['utterance']}")
        print(f"  预期：{step['expect']}")
        print(f"  演示台词：{step['narration']}")
    print()
    print("dry-run 不会调用 build_engine，也不会创建数据库或审计文件。")


def _dry_run_summary() -> dict[str, Any]:
    return {
        "demo": DEMO_NAME,
        "mode": "dry-run",
        "backend": "simulated",
        "boundary": BOUNDARY,
        "writes_files": False,
        "steps": [dict(step) for step in PLAN],
    }


# --------------------------------------------------------------------- CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="demo_showcase.py",
        description=(
            "5 分钟演示：低风险直执行 / 危险动作挂起 / 确认后执行 / "
            "重复不重发 / 审计可导出"
        ),
        epilog=f"{BOUNDARY}；不接真实 HA、不联网、不启用 ASR/TTS。",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印 5 步计划，不建引擎、不写数据库或审计文件",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出机器可读摘要（不含确认令牌）",
    )
    parser.add_argument(
        "--audit-dir",
        metavar="PATH",
        default=None,
        help="把审计 CSV/JSON 导出到该目录（默认写临时目录，演示结束后删除）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    audit_dir = Path(args.audit_dir) if args.audit_dir else None
    if audit_dir is not None and audit_dir.exists() and not audit_dir.is_dir():
        print("参数错误：--audit-dir 必须是目录", file=sys.stderr)
        return 2

    if args.dry_run:
        if args.json:
            print(json.dumps(_dry_run_summary(), ensure_ascii=False, indent=2))
        else:
            _print_dry_run()
        return 0

    try:
        result = run_demo(audit_dir=audit_dir)
    except Exception as exc:  # noqa: BLE001 - 演示脚本：任何启动失败都要给出非 0 退出码
        print(f"演示无法完成：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.summary, ensure_ascii=False, indent=2))
    else:
        _print_live(result)
    return 0 if result.summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())