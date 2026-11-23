"""HA 真机长期 soak：500 次低风险 + 50 次危险挂起/取消。

复用 `scripts/ha_docker_smoke.py` 的 onboarding / REST config flow；
不修改生产代码，只对真实 HA 容器发服务请求并观察 DB/pending/lock。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_docker_smoke as smoke  # noqa: E402

OUT = Path("var/ha-soak")
OUT.mkdir(parents=True, exist_ok=True)
results: list[dict[str, Any]] = []


def record(name: str, ok: bool, actual: str, evidence: dict[str, Any], reason: str = "") -> None:
    results.append(
        {
            "step": name,
            "verdict": "pass" if ok else "fail",
            "actual": actual,
            "evidence": evidence,
            "reason": reason,
        }
    )
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {actual}" + (f" — {reason}" if reason else ""))


def service(name: str, data: dict[str, Any]) -> dict[str, Any]:
    return smoke.service(name, data)


def db_size() -> str:
    proc = smoke.docker("exec", smoke.CONTAINER, "ls", "-l", "/config/home_voice_safety_decisions.db")
    return (proc.stdout + proc.stderr).strip()


def clear_pending() -> int:
    pending = service("list_pending", {})
    items = pending.get("pending") or []
    for item in items:
        service("cancel_action", {"token": item.get("token"), "session_id": item.get("session_id")})
    return len(items)


def main() -> int:
    parser = argparse.ArgumentParser(description="HA 真机长期 soak")
    parser.add_argument("--low-loops", type=int, default=500)
    parser.add_argument("--danger-loops", type=int, default=50)
    parser.add_argument("--session-prefix", default="soak")
    args = parser.parse_args()
    if args.low_loops < 1 or args.danger_loops < 0:
        parser.error("--low-loops 必须 >=1，--danger-loops 必须 >=0")

    smoke.onboard()
    entry = smoke.create_entry()
    if not entry.get("entry") or entry["entry"].get("state") != "loaded":
        record("entry_loaded", False, "entry 未 loaded", {"entry": entry.get("entry")}, "无法继续")
        (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    cleared = clear_pending()
    before_lock = smoke.state_of("lock.front_door")["state"]
    before_db = db_size()

    started = time.time()
    low_dispatch = 0
    for i in range(args.low_loops):
        decision = service(
            "handle_utterance",
            {"text": "开客厅大灯", "session_id": f"{args.session_prefix}-low-{i}"},
        )
        low_dispatch += int(decision.get("dispatch_count") or 0)

    danger_tokens: list[str] = []
    for i in range(args.danger_loops):
        decision = service(
            "handle_utterance",
            {"text": "开大门", "session_id": f"{args.session_prefix}-danger-{i}"},
        )
        token = decision.get("confirmation_token")
        if token:
            danger_tokens.append(str(token))
            service(
                "cancel_action",
                {"token": token, "session_id": f"{args.session_prefix}-danger-{i}"},
            )
    elapsed = round(time.time() - started, 1)

    pending = service("list_pending", {})
    pending_items = pending.get("pending") or []
    after_lock = smoke.state_of("lock.front_door")["state"]
    after_db = db_size()
    audit = service("export_audit", {"format": "json", "redact_utterance": True})
    ok = (
        low_dispatch == args.low_loops
        and len(danger_tokens) == args.danger_loops
        and len(pending_items) == 0
        and before_lock == after_lock == "locked"
    )
    record(
        "soak_long_run",
        ok,
        f"low={low_dispatch}/{args.low_loops} danger={len(danger_tokens)}/{args.danger_loops} pending={len(pending_items)} lock={before_lock}->{after_lock} elapsed={elapsed}s",
        {
            "cleared_history": cleared,
            "low_dispatch": low_dispatch,
            "danger_tokens": len(danger_tokens),
            "pending": pending,
            "lock_before": before_lock,
            "lock_after": after_lock,
            "db_before": before_db,
            "db_after": after_db,
            "audit_rows": audit.get("rows"),
            "elapsed_seconds": elapsed,
        },
        "" if ok else "低风险下发数/危险挂起数/pending/lock 不符合预期",
    )

    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for item in results if item["verdict"] == "pass")
    print(f"\nSoak：{passed}/{len(results)} PASS；明细 {OUT / 'results.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())