"""P0-6：spoken_message 必须是 TTS 安全的（无 token、无 confirm(...)、≤120 字）。

``message`` 可以给程序看，但 ``spoken_message`` 会直接进 TTS / 通知正文。
这里覆盖执行、确认、拒绝、澄清、去重、取消、超时与 sanitizer 兜底。
"""

from __future__ import annotations

import time

from conftest import FixedParser, make_engine, make_settings, step

from hvsl.models import SPOKEN_MESSAGE_LIMIT, Decision, DecisionKind, PlannedCall


def _assert_safe(decision: Decision) -> None:
    spoken = decision.spoken_message
    assert spoken, f"{decision.kind} 缺少 spoken_message"
    assert len(spoken) <= SPOKEN_MESSAGE_LIMIT, spoken
    lowered = spoken.lower()
    assert "confirm(" not in lowered, spoken
    assert "token" not in lowered, spoken
    if decision.confirmation_token:
        assert decision.confirmation_token not in spoken, "令牌不能进 spoken_message"


def test_executed_and_deduplicated_messages_are_safe():
    engine = make_engine(parser=FixedParser([step("hvs.light.turn_on", "客厅大灯")]))
    first = engine.handle("打开客厅大灯")
    second = engine.handle("打开客厅大灯")
    assert first.kind is DecisionKind.EXECUTED
    assert second.kind is DecisionKind.DEDUPLICATED
    _assert_safe(first)
    _assert_safe(second)
    engine.store.close()


def test_confirmation_prompt_is_safe_and_mentions_confirmation():
    engine = make_engine(parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]))
    parked = engine.handle("解锁大门")
    assert parked.kind is DecisionKind.AWAITING_CONFIRMATION
    _assert_safe(parked)
    assert "确认" in parked.spoken_message
    assert "confirm(" not in parked.message.lower()
    assert parked.confirmation_token not in parked.message
    engine.store.close()


def test_reject_and_clarify_messages_are_safe():
    gas = make_engine(parser=FixedParser([step("hvs.switch.turn_on", "switch.gas_valve")]))
    refused = gas.handle("打开燃气阀")
    assert refused.kind is DecisionKind.REJECTED
    _assert_safe(refused)
    gas.store.close()

    ambiguous = make_engine(parser=FixedParser([PlannedCall(tool="hvs.light.turn_on", target="灯")]))
    clarify = ambiguous.handle("把灯打开")
    assert clarify.kind is DecisionKind.CLARIFICATION_REQUIRED
    _assert_safe(clarify)
    ambiguous.store.close()


def test_cancel_confirm_and_expired_messages_are_safe():
    engine = make_engine(parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]))
    parked = engine.handle("解锁大门")
    cancelled = engine.cancel(parked.confirmation_token)
    assert cancelled.kind is DecisionKind.CANCELLED
    _assert_safe(cancelled)

    again = engine.handle("解锁大门")
    confirmed = engine.confirm(again.confirmation_token)
    assert confirmed.kind is DecisionKind.EXECUTED
    _assert_safe(confirmed)
    engine.store.close()

    expiring = make_engine(
        settings=make_settings(confirm_ttl_seconds=0.01),
        parser=FixedParser([step("hvs.lock.unlock", "入户门锁")]),
    )
    late = expiring.handle("解锁大门")
    time.sleep(0.02)
    expired = expiring.confirm(late.confirmation_token)
    assert expired.kind is DecisionKind.REJECTED
    _assert_safe(expired)
    assert "超时" in expired.message
    expiring.store.close()


def test_empty_and_unknown_confirm_messages_are_safe():
    engine = make_engine()
    _assert_safe(engine.handle(""))
    _assert_safe(engine.confirm("not-a-real-token"))
    engine.store.close()


def test_sanitizer_removes_technical_confirmation_syntax():
    decision = Decision(
        trace_id="trace",
        kind=DecisionKind.AWAITING_CONFIRMATION,
        message="确认请调用 confirm(token='abc12345dead')，20 秒内有效。",
        utterance="解锁大门",
    )
    assert "confirm(" not in decision.spoken_message
    assert "abc12345dead" not in decision.spoken_message
    assert len(decision.spoken_message) <= SPOKEN_MESSAGE_LIMIT
    assert "确认" in decision.spoken_message


def test_long_messages_are_truncated_to_the_tts_limit():
    decision = Decision(
        trace_id="trace",
        kind=DecisionKind.EXECUTED,
        message="已执行。" * 60,
        utterance="开灯",
    )
    assert len(decision.spoken_message) <= SPOKEN_MESSAGE_LIMIT
    assert decision.as_dict()["spoken_message"] == decision.spoken_message