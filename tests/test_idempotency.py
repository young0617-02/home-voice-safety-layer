"""A2 (一票否决): the same intent inside the dedup window dispatches exactly once."""

from __future__ import annotations

import threading
import time

from conftest import CATALOG, FixedParser, make_engine, make_settings, step

from hvsl.backends.simulated import SimulatedBackend, load_catalog
from hvsl.errors import BackendUnavailableError, PreDispatchError
from hvsl.models import DecisionKind


class PreDispatchFailOnce(SimulatedBackend):
    """First attempt provably never reached the device, second attempt succeeds."""

    def __init__(self, **kwargs) -> None:
        super().__init__(load_catalog(CATALOG), **kwargs)
        self.attempts = 0

    def call_tool(self, tool, entity_ids, args):
        self.attempts += 1
        if self.attempts == 1:
            raise PreDispatchError("连接设备失败，命令未送达")
        return super().call_tool(tool, entity_ids, args)


class AlwaysUnknownOutcome(SimulatedBackend):
    """The command may or may not have been applied - the safe answer is not to re-send."""

    def __init__(self, **kwargs) -> None:
        super().__init__(load_catalog(CATALOG), **kwargs)
        self.attempts = 0

    def call_tool(self, tool, entity_ids, args):
        self.attempts += 1
        raise BackendUnavailableError("下发后设备无响应，结果未知")


def test_replaying_the_same_intent_three_times_dispatches_once():
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(
        backend=backend, parser=FixedParser([step("hvs.light.turn_on", "客厅大灯", brightness_pct=50)])
    )
    kinds = [engine.handle("开灯").kind for _ in range(3)]
    assert kinds[0] is DecisionKind.EXECUTED
    assert kinds[1] is kinds[2] is DecisionKind.DEDUPLICATED
    assert backend.dispatch_count == 1, "重复指令被重复下发"

    suppressed = engine.audit.query(dispatch_state="suppressed", limit=50)
    assert len(suppressed) == 2
    assert all(row.dedup for row in suppressed)
    assert all(row.idempotency_key for row in suppressed)
    engine.store.close()


def test_different_parameters_are_not_treated_as_duplicates():
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(
        backend=backend, parser=FixedParser([step("hvs.light.turn_on", "客厅大灯", brightness_pct=50)])
    )
    first = engine.handle("开灯到50")
    engine.parser = FixedParser([step("hvs.light.turn_on", "客厅大灯", brightness_pct=80)])
    second = engine.handle("开灯到80")
    assert first.kind is second.kind is DecisionKind.EXECUTED
    assert backend.dispatch_count == 2
    assert second.steps[0].args["brightness_pct"] == 80
    engine.store.close()


def test_different_targets_are_not_treated_as_duplicates():
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(backend=backend, parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]))
    engine.handle("开客厅大灯")
    engine.parser = FixedParser([step("hvs.light.turn_on", "书房台灯")])
    assert engine.handle("开书房台灯").kind is DecisionKind.EXECUTED
    assert backend.dispatch_count == 2
    engine.store.close()


def test_replay_is_allowed_again_after_the_window_expires():
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(
        settings=make_settings(dedup_window_seconds=0.05),
        backend=backend,
        parser=FixedParser([step("hvs.fan.turn_on", "卧室风扇")]),
    )
    engine.handle("开风扇")
    time.sleep(0.1)
    assert engine.handle("开风扇").kind is DecisionKind.EXECUTED
    assert backend.dispatch_count == 2
    engine.store.close()


def test_dangerous_intent_still_dispatches_once_across_two_confirmations():
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(backend=backend, parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]))

    first = engine.handle("解锁大门")
    assert engine.confirm(first.confirmation_token).kind is DecisionKind.EXECUTED
    assert backend.dispatch_count == 1

    second = engine.handle("解锁大门")
    assert second.kind is DecisionKind.AWAITING_CONFIRMATION, "重复的危险指令仍应走确认门禁"
    replayed = engine.confirm(second.confirmation_token)
    assert replayed.kind is DecisionKind.DEDUPLICATED, replayed.message
    assert replayed.dispatch_count == 0
    assert backend.dispatch_count == 1, "同一次危险意图被执行了两次"
    engine.store.close()


def test_pre_dispatch_failure_releases_the_key_so_a_retry_can_run():
    backend = PreDispatchFailOnce()
    engine = make_engine(backend=backend, parser=FixedParser([step("hvs.cover.open", "客厅窗帘")]))
    first = engine.handle("开窗帘")
    assert first.kind is DecisionKind.PARTIAL
    assert backend.attempts == 1
    second = engine.handle("开窗帘")
    assert second.kind is DecisionKind.EXECUTED
    assert backend.attempts == 2
    engine.store.close()


def test_unknown_outcome_is_never_re_issued():
    backend = AlwaysUnknownOutcome()
    engine = make_engine(backend=backend, parser=FixedParser([step("hvs.vacuum.start", "扫地机器人")]))
    first = engine.handle("开始扫地")
    assert first.kind is DecisionKind.PARTIAL
    second = engine.handle("开始扫地")
    # P0-5：未知结果不再伪装成“窗口内重复”，而是显式 in_flight_unknown
    # （failed_unknown / stale_unknown）；两种表达都绝不重发。
    assert second.kind is DecisionKind.PARTIAL, second.message
    assert second.outcomes[0].state == "in_flight_unknown"
    assert backend.attempts == 1, "结果未知的指令被重复下发了"
    engine.store.close()


def test_concurrent_replays_dispatch_once():
    backend = SimulatedBackend.from_catalog(CATALOG)
    engine = make_engine(backend=backend, parser=FixedParser([step("hvs.switch.turn_on", "卧室加湿器")]))
    results: list = []
    barrier = threading.Barrier(3)

    def worker():
        barrier.wait()
        results.append(engine.handle("开加湿器").kind)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(1 for kind in results if kind is DecisionKind.EXECUTED) == 1
    assert backend.dispatch_count == 1
    engine.store.close()
