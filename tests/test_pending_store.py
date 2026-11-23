"""P0-5：待确认动作跨重启持久化 + 幂等 unknown 的人工出口。

这里钉住三个安全事实：

* pending 在进程重启后仍能 confirm（token 不会静默失效）；
* 过期 pending 在 audit 里留下 ``confirmation_state=expired`` / ``error=ttl_expired``；
* “结果未知”的下发永远不能被自动重发，只能通过 ``resolve_unknown`` 留痕处理。
"""

from __future__ import annotations

import time

import pytest
from conftest import CATALOG, FixedParser, make_engine, make_settings, step

from hvsl.backends.simulated import SimulatedBackend, load_catalog
from hvsl.engine import DISPATCH_UNKNOWN_EVENT
from hvsl.errors import BackendUnavailableError
from hvsl.gate import ConfirmationGate
from hvsl.idempotency import GuardResult, IdempotencyGuard
from hvsl.models import DecisionKind
from hvsl.store import Store


class AlwaysUnknownOutcome(SimulatedBackend):
    """命令可能已到达设备：结果未知，安全答案是“不重发”。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(load_catalog(CATALOG), **kwargs)
        self.attempts = 0

    def call_tool(self, tool, entity_ids, args):
        self.attempts += 1
        raise BackendUnavailableError("下发后设备无响应，结果未知")


def test_pending_survives_a_restart_and_can_still_be_confirmed(tmp_path):
    db = tmp_path / "decisions.db"
    settings = make_settings(db_path=str(db))
    planned = [step("hvs.lock.unlock", "入户门锁")]

    first = make_engine(
        settings=settings,
        backend=SimulatedBackend.from_catalog(CATALOG),
        parser=FixedParser(planned),
        store=Store(str(db)),
    )
    parked = first.handle("解锁大门")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION, parked.message
    token = parked.confirmation_token
    assert token
    first.close()  # 模拟 HA 重启：只关闭连接，不清 pending 表

    backend = SimulatedBackend.from_catalog(CATALOG)
    second = make_engine(
        settings=settings,
        backend=backend,
        parser=FixedParser(planned),
        store=Store(str(db)),
    )
    assert len(second.gate) == 0, "未 restore 前内存里不应有旧 token"
    assert second.gate.restore() == 1, "未过期 pending 必须能从 DB 恢复"

    done = second.confirm(token)
    assert done.kind is DecisionKind.EXECUTED, done.message
    assert backend.dispatch_count == 1
    assert second.gate.restore() == 0, "token 已被消费，不能再次恢复"
    second.store.close()


def test_expired_pending_is_dropped_and_audited():
    engine = make_engine(
        settings=make_settings(confirm_ttl_seconds=0.01),
        backend=SimulatedBackend.from_catalog(CATALOG),
        parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]),
    )
    parked = engine.handle("解锁大门")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION

    time.sleep(0.02)
    expired = engine.gate.expire_due()
    assert [action.token for action in expired] == [parked.confirmation_token]
    assert len(engine.gate) == 0

    rows = engine.audit.query(stage="confirm", limit=20)
    expired_rows = [row for row in rows if row.confirmation_state == "expired"]
    assert expired_rows, "过期处置必须在 audit 留下 expired 行"
    assert all(row.error == "ttl_expired" for row in expired_rows)
    assert all(row.risk_class == "door_unlock" for row in expired_rows)

    restored = engine.gate.restore()
    assert restored == 0, "过期条目不得在重启后复活"
    engine.store.close()


def test_stale_in_flight_returns_stale_unknown_and_never_begins():
    store = Store(":memory:")
    guard = IdempotencyGuard(store, window_seconds=30.0, stale_seconds=0.01)
    kwargs = {
        "key": "stale-key",
        "session_id": "default",
        "tool": "hvs.switch.turn_on",
        "targets": ["switch.humidifier"],
        "trace_id": "t1",
    }
    assert guard.try_begin(**kwargs) is GuardResult.BEGIN
    time.sleep(0.02)

    assert guard.try_begin(**kwargs) is GuardResult.STALE_UNKNOWN
    assert guard.try_begin(**kwargs) is GuardResult.STALE_UNKNOWN, "陈旧 unknown 不能被窗口放行"
    assert guard.peek("stale-key")["dispatch_count"] == 0, "unknown 期间绝不允许下发"

    unknown = guard.list_unknown()
    assert [item["key"] for item in unknown] == ["stale-key"]
    assert unknown[0]["stale"] is True
    assert unknown[0]["age_seconds"] >= 0.01
    store.close()


def test_resolve_unknown_only_writes_audit_and_stays_blocked():
    store = Store(":memory:")
    guard = IdempotencyGuard(store, window_seconds=30.0, stale_seconds=120.0)
    kwargs = {
        "key": "unknown-key",
        "session_id": "default",
        "tool": "hvs.vacuum.start",
        "targets": ["vacuum.robot"],
        "trace_id": "trace-unknown",
    }
    assert guard.try_begin(**kwargs) is GuardResult.BEGIN
    guard.mark_failed("unknown-key", reached_backend=True)
    assert guard.peek("unknown-key")["status"] == "failed_unknown"

    assert guard.list_unknown()[0]["key"] == "unknown-key"
    assert guard.resolve_unknown("unknown-key", "现场确认机器人未启动") is True
    assert guard.peek("unknown-key")["status"] == "resolved_unknown", "人工出口只能改状态，不能重发"

    rows = store.query("SELECT * FROM audit WHERE stage = 'result'")
    resolved = [row for row in rows if '"resolved_unknown": true' in (row["detail"] or "")]
    assert len(resolved) == 1
    assert resolved[0]["error"] == "manual_resolved_unknown"
    assert resolved[0]["idempotency_key"] == "unknown-key"

    # 已人工处理的 key 仍然永远不 BEGIN（不会借窗口过期重发）。
    assert guard.try_begin(**kwargs) is GuardResult.STALE_UNKNOWN
    assert guard.list_unknown() == [], "已处理条目不再出现在待处置列表"
    store.close()


def test_failed_confirmations_trigger_a_cooldown():
    gate = ConfirmationGate(max_failed_attempts=5, failure_cooldown_seconds=60.0)
    for index in range(5):
        with pytest.raises(KeyError):
            gate.take("nope", now=1000.0 + index * 0.1)
    with pytest.raises(PermissionError, match="冷却"):
        gate.take("nope", now=1000.5)
    # 冷却期内即使时间前进一点也仍拒绝。
    with pytest.raises(PermissionError):
        gate.take("nope", now=1030.0)
    # 冷却结束后恢复为普通“token 不存在”。
    with pytest.raises(KeyError):
        gate.take("nope", now=1061.0)


def test_engine_reports_stale_unknown_without_redispatch():
    backend = AlwaysUnknownOutcome()
    engine = make_engine(
        settings=make_settings(in_flight_stale_seconds=0.01),
        backend=backend,
        parser=FixedParser([step("hvs.vacuum.start", "扫地机器人")]),
    )
    events: list[tuple[str, dict]] = []
    engine.unknown_event = lambda name, payload: events.append((name, payload))

    first = engine.handle("开始扫地")
    assert first.kind is DecisionKind.PARTIAL
    assert backend.attempts == 1

    second = engine.handle("开始扫地")
    assert second.kind is DecisionKind.PARTIAL, second.message
    assert second.outcomes[0].state == "in_flight_unknown"
    assert backend.attempts == 1, "unknown 结果被自动重发了"

    rows = engine.audit.query(stage="result", dispatch_state="failed_unknown", limit=20)
    assert rows, "stale unknown 必须在 audit 写 failed_unknown 行"
    assert rows[0].error == "stale_unknown"
    assert events and events[0][0] == DISPATCH_UNKNOWN_EVENT
    assert events[0][1]["state"] == "failed_unknown"
    assert events[0][1]["tool"] == "hvs.vacuum.start"
    engine.store.close()