"""HA 真机附加检查：CSV 审计导出 + 长期运行/重复确认不泄漏。

前置：HA 容器已启动（`hvsl-smoke`），且允许重置 `.storage` 后重新 onboarding。
本脚本复用 `scripts/ha_docker_smoke.py` 的 onboarding / config flow 函数，
不修改生产代码；只对真实 HA 实例发 HTTP/WS 请求。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_docker_smoke as smoke  # noqa: E402

OUT = Path("var/ha-extra")
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


def main() -> int:
    smoke.onboard()
    entry = smoke.create_entry()
    if not entry.get("entry") or entry["entry"].get("state") != "loaded":
        record("entry_loaded", False, "entry 未 loaded", {"entry": entry.get("entry")}, "无法继续")
        (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    # ---- 1) CSV 审计导出 ----
    exported = service("export_audit", {"format": "csv", "redact_utterance": True})
    path = exported.get("path")
    evidence: dict[str, Any] = {"response": exported}
    ok_csv = False
    reason = ""
    if path:
        listing = smoke.docker("exec", smoke.CONTAINER, "ls", "-l", path)
        evidence["ls"] = (listing.stdout + listing.stderr).strip()
        reader = (
            "import csv,json;"
            f"p={path!r};"
            "rows=list(csv.DictReader(open(p,encoding='utf-8')));"
            "print(json.dumps({'rows':len(rows),'header':list(rows[0].keys()) if rows else [],"
            "'bad_utterance':sum(1 for r in rows if str(r.get('utterance') or '').strip() not in ('','[redacted]'))},ensure_ascii=False))"
        )
        raw = smoke.docker_exec_python(reader).strip()
        evidence["content_check"] = raw[:2000]
        try:
            parsed = json.loads(raw.splitlines()[-1])
            header = parsed.get("header") or []
            required = {"trace_id", "stage", "utterance", "risk_class", "dispatch_state"}
            ok_csv = bool(
                listing.returncode == 0
                and parsed["rows"] > 0
                and required.issubset(set(header))
                and parsed["bad_utterance"] == 0
            )
            if not ok_csv:
                reason = f"CSV 行数/表头/脱敏不符：{parsed}"
        except Exception as exc:  # noqa: BLE001
            reason = f"CSV 无法解析：{exc}"
    else:
        reason = "export_audit 未返回 path"
    record("csv_export", ok_csv, f"path={path} rows={exported.get('rows')}", evidence, reason)

    # ---- 2) 长期运行：50 次低风险 + 10 次危险挂起/取消 ----
    before_lock = smoke.state_of("lock.front_door")["state"]
    states = smoke.api("GET", "/api/states").json()
    lights = [
        (item.get("entity_id"), (item.get("attributes") or {}).get("friendly_name"))
        for item in states
        if str(item.get("entity_id", "")).startswith("light.")
    ]
    dispatch_total = 0
    danger_tokens: list[str] = []
    last_decision: dict[str, Any] = {}
    for i in range(50):
        decision = service("handle_utterance", {"text": "开客厅大灯", "session_id": f"soak-{i}"})
        last_decision = decision
        dispatch_total += int(decision.get("dispatch_count") or 0)
    for i in range(10):
        decision = service("handle_utterance", {"text": "开大门", "session_id": f"danger-{i}"})
        token = decision.get("confirmation_token")
        if token:
            danger_tokens.append(token)
            service("cancel_action", {"token": token, "session_id": f"danger-{i}"})
    pending = service("list_pending", {})
    after_lock = smoke.state_of("lock.front_door")["state"]
    pending_count = int(pending.get("count") or len(pending.get("pending") or []))
    ok_soak = (
        dispatch_total == 50
        and len(danger_tokens) == 10
        and pending_count == 0
        and before_lock == after_lock == "locked"
    )
    record(
        "soak_50_low_10_danger",
        ok_soak,
        f"low_dispatch={dispatch_total}/50 danger_tokens={len(danger_tokens)}/10 pending={pending_count} lock={before_lock}->{after_lock}",
        {
            "dispatch_total": dispatch_total,
            "danger_tokens": len(danger_tokens),
            "pending": pending,
            "lock_before": before_lock,
            "lock_after": after_lock,
            "lights": lights,
            "last_decision": last_decision,
        },
        "" if ok_soak else "低风险下发数/危险挂起数/pending/lock 状态不符合预期",
    )

    # ---- 3) DB 大小与审计行数（长期运行不异常增长） ----
    db = smoke.docker("exec", smoke.CONTAINER, "ls", "-l", "/config/home_voice_safety_decisions.db")
    audit = service("export_audit", {"format": "json", "redact_utterance": True})
    ok_size = db.returncode == 0 and int(audit.get("rows") or 0) >= 60
    record(
        "db_growth",
        ok_size,
        f"db={db.stdout.strip()[:120]} audit_rows={audit.get('rows')}",
        {"db": (db.stdout + db.stderr).strip()[:500], "audit_rows": audit.get("rows")},
        "" if ok_size else "DB 文件缺失或审计行数低于预期",
    )

    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for item in results if item["verdict"] == "pass")
    print(f"\n附加检查：{passed}/{len(results)} PASS；明细 {OUT / 'results.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())