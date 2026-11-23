"""HA 真机闭环：blueprint/通知动作 -> confirm_action / cancel_action。

前置：`hvsl-smoke` 容器已启动，blueprint 与 automations.yaml 已写入挂载配置。
本脚本复用 `scripts/ha_docker_smoke.py` 的 onboarding / REST config flow，
只做真实 HTTP 事件触发与状态校验，不改生产代码。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ha_docker_smoke as smoke  # noqa: E402

OUT = Path("var/ha-confirm")
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


def fire_event(action: str) -> int:
    response = smoke.api(
        "POST",
        "/api/events/mobile_app_notification_action",
        json={"action": action},
    )
    return response.status_code


def wait_lock(expected: str, timeout: float = 12.0) -> str:
    deadline = time.time() + timeout
    state = smoke.state_of("lock.front_door")["state"]
    while time.time() < deadline:
        state = smoke.state_of("lock.front_door")["state"]
        if state == expected:
            return state
        time.sleep(0.5)
    return state


def clear_pending() -> int:
    """取消历史残留 pending（DB 跨重启保留），保证本轮断言只受本轮 token 影响。"""
    pending = service("list_pending", {})
    items = pending.get("pending") or []
    for item in items:
        service(
            "cancel_action",
            {"token": item.get("token"), "session_id": item.get("session_id")},
        )
    return len(items)


def main() -> int:
    smoke.onboard()
    entry = smoke.create_entry()
    if not entry.get("entry") or entry["entry"].get("state") != "loaded":
        record("entry_loaded", False, "entry 未 loaded", {"entry": entry.get("entry")}, "无法继续")
        (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1

    cleared = clear_pending()
    automation = smoke.api("GET", "/api/states/automation.hvs_confirm_action")
    record(
        "blueprint_automation_loaded",
        automation.status_code == 200,
        f"automation.hvs_confirm_action -> {automation.status_code}",
        {"body": automation.text[:500]},
        "" if automation.status_code == 200 else "blueprint 自动化未加载",
    )
    # HA 重启后 automation trigger 注册可能晚于 state=on；等它 ready 再触发事件。
    time.sleep(2.0)

    before = smoke.state_of("lock.front_door")["state"]
    decision = service("handle_utterance", {"text": "开大门", "session_id": "confirm-loop"})
    token = str(decision.get("confirmation_token") or "")
    fire_status = fire_event(f"HVS_CONFIRM:{token}:confirm-loop") if token else -1
    time.sleep(1.0)
    automation_after = smoke.api("GET", "/api/states/automation.hvs_confirm_action")
    after = wait_lock("unlocked")
    ok_confirm = bool(before == "locked" and token and fire_status in (200, 201) and after == "unlocked")
    record(
        "notification_action_confirms",
        ok_confirm,
        f"before={before} token={'yes' if token else 'no'} event={fire_status} after={after}",
        {
            "decision": decision,
            "fire_status": fire_status,
            "lock_before": before,
            "lock_after": after,
            "automation_after": automation_after.text[:500],
        },
        "" if ok_confirm else "通知动作未能触发 confirm_action 或 lock 未变化",
    )

    # 取消闭环：先把 lock 复位为 locked，再挂起危险动作并触发 HVS_CANCEL。
    smoke.api("POST", "/api/services/lock/lock", json={"entity_id": "lock.front_door"})
    time.sleep(1.0)
    before_cancel = smoke.state_of("lock.front_door")["state"]
    decision2 = service("handle_utterance", {"text": "开大门", "session_id": "cancel-loop"})
    token2 = str(decision2.get("confirmation_token") or "")
    cancel_status = fire_event(f"HVS_CANCEL:{token2}:cancel-loop") if token2 else -1
    time.sleep(1.5)
    after_cancel = smoke.state_of("lock.front_door")["state"]
    pending = service("list_pending", {})
    pending_items = pending.get("pending") or []
    pending_tokens = {str(item.get("token") or "") for item in pending_items}
    pending_count = int(pending.get("count") or len(pending_items))
    ok_cancel = bool(
        token2
        and cancel_status in (200, 201)
        and after_cancel == before_cancel == "locked"
        and token2 not in pending_tokens
        and pending_count == 0
    )
    record(
        "notification_action_cancels",
        ok_cancel,
        f"before={before_cancel} token={'yes' if token2 else 'no'} event={cancel_status} after={after_cancel} pending={pending_count}",
        {
            "decision": decision2,
            "fire_status": cancel_status,
            "lock_before": before_cancel,
            "lock_after": after_cancel,
            "pending": pending,
        },
        "" if ok_cancel else "取消动作未清空 pending 或 lock 被改变",
    )

    (OUT / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    passed = sum(1 for item in results if item["verdict"] == "pass")
    print(f"\nBlueprint 闭环：{passed}/{len(results)} PASS；明细 {OUT / 'results.json'}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())