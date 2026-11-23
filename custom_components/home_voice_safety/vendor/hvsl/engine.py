"""The decision chain: utterance -> intent -> tool -> safety gate -> dispatch -> audit.

This module is the product of the project; ASR/TTS and the Home Assistant
integration are carriers for it. The invariants it must hold:

1. If any step of a plan is dangerous, *no* step is dispatched until the user
   confirms the whole plan (F-007).
2. A backend call happens only while holding an idempotency claim, and the audit
   row is written before the call and updated after it (F-008/F-009).
3. Unknown tools, unnamespaced tools, unmatched targets and out-of-policy
   requests are refused rather than guessed at.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, Protocol, cast

from . import guards
from .audit import AuditLog
from .backends.base import DeviceBackend
from .config import AuditRecord, Settings
from .errors import (
    AmbiguousTargetError,
    BackendUnavailableError,
    ConfirmationExpiredError,
    DispatchTimeoutError,
    HvslError,
    NoTargetError,
    PreDispatchError,
    ToolValidationError,
    UnknownToolError,
)
from .gate import ConfirmationGate
from .idempotency import GuardResult, IdempotencyGuard
from .models import (
    ConfirmationState,
    Decision,
    DecisionKind,
    Intent,
    PendingAction,
    ResolvedStep,
    RiskLevel,
    StepOutcome,
    idempotency_key,
    token_hash,
)
from .resolver import TargetResolver
from .risk import RiskPolicy, combine
from .store import Store
from .tools import ToolRegistry, build_default_registry

CONFIRM_WORDS = {"确认", "确定", "是的", "是", "执行吧", "继续", "confirm", "yes", "y", "ok"}
CANCEL_WORDS = {"取消", "算了", "不用了", "别", "cancel", "no", "n"}
#: 结果未知的下发只做旁路通知；HA 通过 ``hass.bus.async_fire`` 监听同名事件。
DISPATCH_UNKNOWN_EVENT = "home_voice_safety_dispatch_unknown"


class IntentParser(Protocol):
    def parse(self, utterance: str) -> Intent: ...


class DecisionEngine:
    def __init__(
        self,
        backend: DeviceBackend,
        parser: IntentParser,
        *,
        settings: Settings | None = None,
        registry: ToolRegistry | None = None,
        policy: RiskPolicy | None = None,
        store: Store | None = None,
        cross_check: IntentParser | None = None,
        unknown_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.backend = backend
        self.parser = parser
        #: 确定性影子解析器：模型给的计划要用它兜住“替用户消歧”这一类越权。
        self.cross_check = cross_check
        #: 结果未知（stale/unknown）的旁路事件钩子；默认不装，CLI 无事件总线。
        self.unknown_event = unknown_event
        self.registry = registry or build_default_registry()
        self.policy = policy or RiskPolicy.load(self.settings.policy_path)
        self.store = store or Store(self.settings.db_path)
        self.audit = AuditLog(self.store)
        self.idempotency = IdempotencyGuard(
            self.store,
            window_seconds=self.settings.dedup_window_seconds,
            stale_seconds=self.settings.in_flight_stale_seconds,
        )
        # 注入 store/audit：park/take/drop/expire_due 同步落 pending 表，expire 可留痕（P0-5）。
        self.gate = ConfirmationGate(
            self.settings.confirm_ttl_seconds,
            self.settings.max_pending_actions,
            store=self.store,
            audit=self.audit,
        )
        self.resolver = TargetResolver(backend)

    # ---------------------------------------------------------------- public API

    def handle(self, utterance: str, *, session_id: str = "default") -> Decision:
        trace_id = Decision.new_trace_id()
        started = time.perf_counter()
        text = (utterance or "").strip()
        self._log(trace_id, session_id, stage="utterance", utterance=text)

        if not text:
            return self._reject(trace_id, session_id, text, "空指令：没有收到可解析的内容", started=started)

        refusal = self.policy.refusal_for(text)
        if refusal is not None:
            return self._reject(
                trace_id, session_id, text, f"[{refusal.id}] {refusal.reason}", started=started
            )

        # 定时/延后执行类请求在任何解析器之前拒绝：不能把“明天的事”现在就办掉。
        late = guards.unsupported_reason(text)
        if late is not None:
            return self._reject(
                trace_id,
                session_id,
                text,
                f"[unsupported_schedule] 句子里的时间表达「{late}」不在范围内："
                "本层只处理即时指令，不做定时任务",
                started=started,
            )

        normalised = text.strip("。！!？?，, ").lower()
        if normalised in CONFIRM_WORDS:
            return self.confirm(session_id=session_id, user_words=text)
        if normalised in CANCEL_WORDS:
            return self.cancel(session_id=session_id, user_words=text)

        try:
            parse_started = time.perf_counter()
            intent = self.parser.parse(text)
            nlu_ms = (time.perf_counter() - parse_started) * 1000.0
        except HvslError as exc:
            return self._reject(trace_id, session_id, text, f"意图解析失败：{exc}", started=started)

        # 先纠偏再留痕：审计里记的必须是真正参与决策的那份计划（含被否决的原因）。
        intent = guards.clamp_plan(intent)
        intent = self._cross_check(intent)

        self._log(
            trace_id,
            session_id,
            stage="nlu",
            utterance=text,
            intent=intent.name,
            nlu_provider=intent.provider,
            degraded=intent.degraded,
            latency_ms=round(nlu_ms, 3),
            detail={"steps": [step.model_dump() for step in intent.steps], "notes": intent.notes},
        )

        if not intent.steps:
            return self._reject(
                trace_id,
                session_id,
                text,
                "无法解析为受支持的设备指令（本助手只控制受控清单内的设备）",
                intent=intent.name,
                started=started,
            )

        try:
            steps = self._plan(intent)
        except AmbiguousTargetError as exc:
            return self._stamp(
                self._clarify(trace_id, session_id, text, str(exc), exc.candidates, steps=[], started=started),
                intent,
            )
        except (UnknownToolError, ToolValidationError, NoTargetError, BackendUnavailableError, DispatchTimeoutError) as exc:
            return self._stamp(
                self._reject(trace_id, session_id, text, str(exc), intent=intent.name, started=started), intent
            )

        plan_risk = combine(*(step.risk_level for step in steps))
        denied = _first_risk(steps, RiskLevel.DENY)
        if plan_risk is RiskLevel.DENY and denied is not None:
            return self._reject(
                trace_id,
                session_id,
                text,
                f"[{denied.risk_class}] {denied.reason or '该动作被策略直接拒绝'}",
                intent=intent.name,
                steps=steps,
                started=started,
            )

        if plan_risk is RiskLevel.CONFIRM:
            return self._park(trace_id, session_id, text, intent, steps, started=started)

        return self._execute(
            steps,
            trace_id=trace_id,
            session_id=session_id,
            utterance=text,
            intent_name=intent.name,
            confirmation_state=ConfirmationState.NOT_REQUIRED,
            nlu_provider=intent.provider,
            degraded=intent.degraded,
            timings={"nlu": round(nlu_ms, 3)},
            started=started,
        )

    def confirm(
        self,
        token: str | None = None,
        *,
        session_id: str = "default",
        user_words: str = "",
    ) -> Decision:
        """Release a parked plan. This is the only path that dispatches dangerous steps."""
        started = time.perf_counter()
        pending = self._resolve_pending(token, session_id)
        if pending is None:
            return self._reject(
                Decision.new_trace_id(),
                session_id,
                user_words,
                "当前会话没有等待确认的指令，未向设备下发任何东西。",
                started=started,
            )
        if isinstance(pending, Decision):
            return pending

        trace_id = pending.trace_id or Decision.new_trace_id()
        try:
            action = self.gate.take(pending.token, session_id=session_id)
        except ConfirmationExpiredError as exc:
            self._log(
                trace_id,
                session_id,
                stage="confirm",
                utterance=user_words or action_utterance(pending),
                confirmation_state=ConfirmationState.EXPIRED.value,
                risk_level=RiskLevel.CONFIRM.value,
                risk_class=pending.risk_class,
                error=str(exc),
            )
            return self._reject(
                trace_id,
                session_id,
                action_utterance(pending),
                "确认已超时，指令未下发。请重新发起指令。",
                started=started,
                confirmation_state=ConfirmationState.EXPIRED,
            )
        except (KeyError, PermissionError, ValueError):
            return self._reject(
                trace_id,
                session_id,
                action_utterance(pending),
                "确认无效：令牌不存在、不属于当前会话或已失效。",
                started=started,
            )

        refusal = self.policy.refusal_for(action.utterance)
        if refusal is not None:
            self.gate.drop(action.token)
            return self._reject(
                trace_id,
                session_id,
                action.utterance,
                f"[{refusal.id}] {refusal.reason}",
                started=started,
                confirmation_state=ConfirmationState.DENIED,
            )

        try:
            steps = self._revalidate(action)
        except (
            NoTargetError,
            UnknownToolError,
            ToolValidationError,
            BackendUnavailableError,
            DispatchTimeoutError,
        ) as exc:
            return self._reject(
                trace_id,
                session_id,
                action.utterance,
                f"确认时重新校验失败：{exc}",
                started=started,
                confirmation_state=ConfirmationState.DENIED,
            )

        if combine(*(step.risk_level for step in steps)) is RiskLevel.DENY:
            step = _first_risk(steps, RiskLevel.DENY)
            assert step is not None
            return self._reject(
                trace_id,
                session_id,
                action.utterance,
                f"[{step.risk_class}] 策略已升级为直接拒绝，指令未下发",
                steps=steps,
                started=started,
                confirmation_state=ConfirmationState.DENIED,
            )

        self._log(
            trace_id,
            session_id,
            stage="confirm",
            utterance=action.utterance,
            confirmation_state=ConfirmationState.CONFIRMED.value,
            risk_level=RiskLevel.CONFIRM.value,
            risk_class=action.risk_class,
            tool=steps[0].tool if steps else "",
            targets=[t for s in steps for t in s.targets],
            detail={"pending_token_hash": token_hash(action.token), "confirmed_by": user_words or "token"},
        )

        return self._execute(
            steps,
            trace_id=trace_id,
            session_id=session_id,
            utterance=action.utterance,
            intent_name="confirmed_plan",
            confirmation_state=ConfirmationState.CONFIRMED,
            nlu_provider="gate",
            degraded=False,
            timings={},
            started=started,
        )

    def cancel(self, token: str | None = None, *, session_id: str = "default", user_words: str = "") -> Decision:
        started = time.perf_counter()
        pending = self._resolve_pending(token, session_id)
        if pending is None:
            return self._reject(
                Decision.new_trace_id(),
                session_id,
                user_words,
                "当前会话没有等待确认的指令，无需取消。",
                started=started,
            )
        if isinstance(pending, Decision):
            return pending
        action = self.gate.drop(pending.token)
        trace_id = (action.trace_id if action else None) or pending.trace_id or Decision.new_trace_id()
        utterance = (action.utterance if action else pending.utterance)
        self._log(
            trace_id,
            session_id,
            stage="cancel",
            utterance=utterance,
            confirmation_state=ConfirmationState.CANCELLED.value,
            risk_level=RiskLevel.CONFIRM.value,
            risk_class=pending.risk_class,
            tool=pending.steps[0].tool if pending.steps else "",
            targets=[t for s in pending.steps for t in s.targets],
        )
        return Decision(
            trace_id=trace_id,
            kind=DecisionKind.CANCELLED,
            message="已取消，未向设备下发任何指令。",
            utterance=utterance,
            session_id=session_id,
            steps=pending.steps,
            risk_level=RiskLevel.CONFIRM,
            dispatch_count=0,
            timings_ms={"total": round((time.perf_counter() - started) * 1000.0, 3)},
        )

    def pending(self, session_id: str = "default") -> list[PendingAction]:
        return self.gate.for_session(session_id)

    def trace(self, trace_id: str) -> list[AuditRecord]:
        return self.audit.by_trace(trace_id)

    def stats(self) -> dict[str, int]:
        """P1-1: aggregate in SQL (GROUP BY), never load 100k audit rows into Python."""
        counts = self.audit.stats()
        return {
            "dispatched": int(counts.get("executed", 0)),
            "suppressed": int(counts.get("suppressed", 0)),
            "failed": int(counts.get("failed", 0)),
            "pending": len(self.gate),
        }

    def close(self) -> None:
        self.store.close()
        self.backend.close()

    # ---------------------------------------------------------------- internals

    def _cross_check(self, intent: Intent) -> Intent:
        """纠偏：模型给的方案必须经过确定性解析的对表。

        只纠正两类越权：① 目标设备名在用户原话里根本没有依据（替用户消歧 / 凭空造设备）；
        ② 与确定性解析不一致的批量位（已在 clamp_plan 里按原话处理）。

        是否纠偏只看有没有装配 ``cross_check``，不再看 ``provider`` 标签：
        LLM 降级后会标成 ``rule``，按标签跳过会把整条 HA LLM 路径的纠偏关掉（F3）。
        """
        if self.cross_check is None or not intent.steps:
            return intent
        try:
            shadow = guards.clamp_plan(self.cross_check.parse(intent.utterance))
        except HvslError:
            return intent
        fallback: dict[str, str] = {}
        for step in shadow.steps:
            if self.registry.has(step.tool):
                fallback.setdefault(step.tool, step.target)
        names_for_id = {
            entity.entity_id: [entity.display_name, *entity.aliases] for entity in self.backend.entities()
        }
        steps, notes = guards.grounding_veto(intent.steps, intent.utterance, fallback, names_for_id)
        intent.steps = steps
        if notes:
            intent.notes.extend(notes)
            intent.degraded = True
        return intent

    def _plan(self, intent: Intent) -> list[ResolvedStep]:
        steps: list[ResolvedStep] = []
        for planned in intent.steps:
            tool = self.registry.get(planned.tool)
            entity_ids = self.resolver.resolve(planned, tool, grounding_text=intent.utterance)
            entities = [e for e in (self.backend.get(entity_id) for entity_id in entity_ids) if e is not None]
            if not entities:
                raise NoTargetError(f"解析出的实体 {entity_ids} 已不在受控清单内")
            args = {**planned.args, "entity_ids": entity_ids}
            validated = self.registry.validate_args(tool.name, args)
            risk, risk_class, reason = self.policy.classify(tool.name, entities)
            steps.append(
                ResolvedStep(
                    tool=tool.name,
                    args=validated,
                    targets=entity_ids,
                    risk_level=risk,
                    risk_class=risk_class,
                    reason=reason,
                )
            )
        return steps

    def _revalidate(self, action: PendingAction) -> list[ResolvedStep]:
        """Re-check the parked plan against the *current* device world before dispatch."""
        fresh: list[ResolvedStep] = []
        for step in action.steps:
            tool = self.registry.get(step.tool)
            entities = [e for e in (self.backend.get(t) for t in step.targets) if e is not None]
            if len(entities) != len(step.targets):
                missing = set(step.targets) - {e.entity_id for e in entities}
                raise NoTargetError(f"实体已消失：{sorted(missing)}")
            risk, risk_class, reason = self.policy.classify(tool.name, entities)
            args = self.registry.validate_args(tool.name, step.args)
            fresh.append(
                ResolvedStep(
                    tool=tool.name,
                    args=args,
                    targets=list(step.targets),
                    risk_level=risk,
                    risk_class=risk_class,
                    reason=reason,
                )
            )
        return fresh

    def _park(
        self,
        trace_id: str,
        session_id: str,
        text: str,
        intent: Intent,
        steps: list[ResolvedStep],
        *,
        started: float,
    ) -> Decision:
        confirm_step = _first_risk(steps, RiskLevel.CONFIRM)
        assert confirm_step is not None
        pending = self.gate.park(
            session_id=session_id,
            utterance=text,
            steps=steps,
            risk_class=confirm_step.risk_class,
            trace_id=trace_id,
        )
        pending.trace_id = trace_id
        self._log(
            trace_id,
            session_id,
            stage="confirm",
            utterance=text,
            intent=intent.name,
            confirmation_state=ConfirmationState.PENDING.value,
            risk_level=RiskLevel.CONFIRM.value,
            risk_class=pending.risk_class,
            tool=steps[0].tool,
            targets=[t for s in steps for t in s.targets],
            nlu_provider=intent.provider,
            degraded=intent.degraded,
            detail={"pending_token_hash": token_hash(pending.token), "ttl_seconds": self.settings.confirm_ttl_seconds},
        )
        return Decision(
            trace_id=trace_id,
            kind=DecisionKind.AWAITING_CONFIRMATION,
            message=self._confirm_message(steps, pending),
            utterance=text,
            session_id=session_id,
            intent=intent.name,
            steps=steps,
            confirmation_token=pending.token,
            risk_level=RiskLevel.CONFIRM,
            dispatch_count=0,
            nlu_provider=intent.provider,
            degraded=intent.degraded,
            timings_ms={"total": round((time.perf_counter() - started) * 1000.0, 3)},
        )

    def _confirm_message(self, steps: list[ResolvedStep], pending: PendingAction) -> str:
        targets = "、".join(self._display(t) for s in steps for t in s.targets)
        # P0-6：面向 TTS / 通知的文案，绝不包含 confirm(token='...')。
        # 技术令牌只通过 Decision.confirmation_token 交给程序/自动化。
        return (
            f"【{targets}】属于危险操作，需要你确认。"
            f"{self.settings.confirm_ttl_seconds:.0f} 秒内未确认将自动作废，"
            "确认前不会下发任何指令。"
        )

    def _display(self, entity_id: str) -> str:
        entity = self.backend.get(entity_id)
        return entity.display_name if entity else entity_id

    def _fire_dispatch_unknown(
        self,
        *,
        key: str,
        step: ResolvedStep,
        session_id: str,
        trace_id: str,
        utterance: str,
    ) -> None:
        """旁路通知“结果未知”，事件失败绝不影响安全决策本身。"""
        if self.unknown_event is None:
            return
        payload: dict[str, Any] = {
            "idempotency_key": key,
            "tool": step.tool,
            "targets": list(step.targets),
            "session_id": session_id,
            "trace_id": trace_id,
            "utterance": utterance,
            "state": "failed_unknown",
        }
        try:
            self.unknown_event(DISPATCH_UNKNOWN_EVENT, payload)
        except Exception as exc:  # noqa: BLE001 - 事件总线不可用不能拖垮决策链
            self._log(
                trace_id,
                session_id,
                stage="result",
                tool=step.tool,
                targets=step.targets,
                idempotency_key=key,
                detail={"event_error": str(exc)},
            )

    def _execute(
        self,
        steps: list[ResolvedStep],
        *,
        trace_id: str,
        session_id: str,
        utterance: str,
        intent_name: str,
        confirmation_state: ConfirmationState,
        nlu_provider: str,
        degraded: bool,
        timings: dict[str, float],
        started: float,
    ) -> Decision:
        outcomes: list[StepOutcome] = []
        dispatch_ms = 0.0
        for step in steps:
            tool = self.registry.get(step.tool)
            # Relative adjustments are explicitly non-idempotent: saying "调暗一点"
            # twice means two steps.  The trace id makes each intent its own key so
            # the dedup window cannot collapse the second one.  Every other tool -
            # including every dangerous action - keeps the original key.
            key_session = session_id
            if tool is not None and tool.idempotent is False:
                key_session = f"{session_id}#{trace_id}"
            key = idempotency_key(key_session, step.tool, step.args, step.targets)
            claim = self.idempotency.try_begin(
                key=key,
                session_id=session_id,
                tool=step.tool,
                targets=step.targets,
                trace_id=trace_id,
            )
            if claim is not GuardResult.BEGIN:
                unknown = claim in (GuardResult.IN_FLIGHT, GuardResult.STALE_UNKNOWN)
                state = "in_flight_unknown" if unknown else "suppressed"
                if claim is GuardResult.STALE_UNKNOWN:
                    # 陈旧 in_flight 固化为 failed_unknown：结果未知，永不自动重发。
                    self.idempotency.mark_failed(key, reached_backend=True)
                self._log(
                    trace_id,
                    session_id,
                    stage="dispatch",
                    utterance=utterance,
                    intent=intent_name,
                    tool=step.tool,
                    targets=step.targets,
                    risk_level=step.risk_level.value,
                    risk_class=step.risk_class,
                    confirmation_state=confirmation_state.value,
                    dispatch_state=state,
                    idempotency_key=key,
                    dedup=True,
                    nlu_provider=nlu_provider,
                    degraded=degraded,
                    detail={"claim": claim.value},
                )
                if claim is GuardResult.STALE_UNKNOWN:
                    # 留一条 result 行把“未知”显式写进审计，并通知 HA。
                    self._log(
                        trace_id,
                        session_id,
                        stage="result",
                        utterance=utterance,
                        intent=intent_name,
                        tool=step.tool,
                        targets=step.targets,
                        risk_level=step.risk_level.value,
                        risk_class=step.risk_class,
                        confirmation_state=confirmation_state.value,
                        dispatch_state="failed_unknown",
                        idempotency_key=key,
                        nlu_provider=nlu_provider,
                        degraded=degraded,
                        error="stale_unknown",
                        detail={"claim": claim.value},
                    )
                    self._fire_dispatch_unknown(
                        key=key,
                        step=step,
                        session_id=session_id,
                        trace_id=trace_id,
                        utterance=utterance,
                    )
                if claim is GuardResult.STALE_UNKNOWN:
                    message = (
                        "上次下发超过陈旧阈值仍未知（stale_unknown），未再次下发；"
                        "请用 list_unknown/resolve_unknown 人工处置"
                    )
                elif claim is GuardResult.IN_FLIGHT:
                    message = "上次下发结果未知（in_flight），未再次下发"
                else:
                    message = "重复指令在去重窗口内被抑制，未再次下发"
                outcomes.append(
                    StepOutcome(
                        tool=step.tool,
                        targets=step.targets,
                        dispatched=False,
                        state=cast(Any, state),
                        error=message,
                        idempotency_key=key,
                    )
                )
                continue

            row_id = self.audit.append(
                AuditRecord(
                    trace_id=trace_id,
                    session_id=session_id,
                    ts=Store.now(),
                    stage="dispatch",
                    utterance=utterance,
                    intent=intent_name,
                    tool=step.tool,
                    targets=step.targets,
                    risk_level=step.risk_level.value,
                    risk_class=step.risk_class,
                    confirmation_state=confirmation_state.value,
                    dispatch_state="DISPATCHING",
                    idempotency_key=key,
                    nlu_provider=nlu_provider,
                    degraded=degraded,
                    detail={"args": step.args},
                )
            )
            call_started = time.perf_counter()
            try:
                self.backend.call_tool(tool, step.targets, step.args)
            except PreDispatchError as exc:
                elapsed = round((time.perf_counter() - call_started) * 1000.0, 3)
                dispatch_ms += elapsed
                self.audit.finalize(row_id, "failed", error=str(exc), latency_ms=elapsed)
                self.idempotency.mark_failed(key, reached_backend=False)
                outcomes.append(
                    StepOutcome(
                        tool=step.tool,
                        targets=step.targets,
                        dispatched=False,
                        state="failed",
                        # A deterministic refusal (off + dim, missing attribute,
                        # unavailable state, delta=0) must not be advertised as a
                        # retry: retrying cannot change the answer.  The dispatch
                        # state machine stays "failed" so existing metrics keep
                        # their meaning (see README/C端路线 note).
                        error=f"未送达设备，可重试：{exc}"
                        if exc.retryable
                        else f"未下发：{exc}",
                        latency_ms=elapsed,
                        idempotency_key=key,
                    )
                )
                continue
            except (DispatchTimeoutError, BackendUnavailableError) as exc:
                elapsed = round((time.perf_counter() - call_started) * 1000.0, 3)
                dispatch_ms += elapsed
                self.audit.finalize(row_id, "failed", error=str(exc), latency_ms=elapsed)
                self.idempotency.mark_failed(key, reached_backend=True)
                outcomes.append(
                    StepOutcome(
                        tool=step.tool,
                        targets=step.targets,
                        dispatched=True,
                        state="failed",
                        error=str(exc),
                        latency_ms=elapsed,
                        idempotency_key=key,
                    )
                )
                continue
            except Exception as exc:  # noqa: BLE001 - unexpected backend blow-up is still an unknown outcome
                elapsed = round((time.perf_counter() - call_started) * 1000.0, 3)
                dispatch_ms += elapsed
                self.audit.finalize(row_id, "failed", error=f"unexpected: {exc}", latency_ms=elapsed)
                self.idempotency.mark_failed(key, reached_backend=True)
                outcomes.append(
                    StepOutcome(
                        tool=step.tool,
                        targets=step.targets,
                        dispatched=True,
                        state="failed",
                        error=str(exc),
                        latency_ms=elapsed,
                        idempotency_key=key,
                    )
                )
                continue

            elapsed = round((time.perf_counter() - call_started) * 1000.0, 3)
            dispatch_ms += elapsed
            self.audit.finalize(row_id, "executed", latency_ms=elapsed)
            self.idempotency.mark_executed(key)
            outcomes.append(
                StepOutcome(
                    tool=step.tool,
                    targets=step.targets,
                    dispatched=True,
                    state="executed",
                    latency_ms=elapsed,
                    idempotency_key=key,
                )
            )

        executed = sum(1 for o in outcomes if o.state == "executed")
        failed = sum(1 for o in outcomes if o.state in ("failed", "in_flight_unknown"))
        suppressed = sum(1 for o in outcomes if o.state == "suppressed")
        if executed and failed:
            kind = DecisionKind.PARTIAL
        elif executed:
            kind = DecisionKind.EXECUTED
        elif suppressed and not failed:
            kind = DecisionKind.DEDUPLICATED
        else:
            kind = DecisionKind.PARTIAL if failed else DecisionKind.REJECTED

        return Decision(
            trace_id=trace_id,
            kind=kind,
            message=self._execute_message(outcomes, confirmation_state),
            utterance=utterance,
            session_id=session_id,
            intent=intent_name,
            steps=steps,
            outcomes=outcomes,
            risk_level=combine(*(s.risk_level for s in steps)),
            dispatch_count=executed,
            nlu_provider=nlu_provider,
            degraded=degraded,
            timings_ms={
                **timings,
                "dispatch": round(dispatch_ms, 3),
                "total": round((time.perf_counter() - started) * 1000.0, 3),
            },
        )

    def _execute_message(self, outcomes: list[StepOutcome], confirmation_state: ConfirmationState) -> str:
        parts: list[str] = []
        for outcome in outcomes:
            targets = "、".join(self._display(t) for t in outcome.targets)
            if outcome.state == "executed":
                parts.append(f"已执行 {outcome.tool} -> {targets}")
            elif outcome.state == "suppressed":
                parts.append(f"已去重（未再次下发）{outcome.tool} -> {targets}")
            elif outcome.state == "in_flight_unknown":
                parts.append(f"上次下发结果未知，为避免重复未再次下发 {outcome.tool} -> {targets}")
            elif outcome.state == "failed":
                parts.append(f"下发失败 {outcome.tool} -> {targets}：{outcome.error}")
        prefix = "（已确认）" if confirmation_state is ConfirmationState.CONFIRMED else ""
        return prefix + "；".join(parts) if parts else "没有任何指令被下发。"

    def _resolve_pending(self, token: str | None, session_id: str):
        if token:
            action = self.gate.get(token)
            if action is None:
                return self._reject(
                    Decision.new_trace_id(),
                    session_id,
                    "",
                    "确认无效：令牌不存在、已过期或已被使用",
                    started=time.perf_counter(),
                    confirmation_state=ConfirmationState.DENIED,
                )
            return action
        pendings = self.gate.for_session(session_id)
        if not pendings:
            return None
        if len(pendings) > 1:
            return Decision(
                trace_id=Decision.new_trace_id(),
                kind=DecisionKind.CLARIFICATION_REQUIRED,
                message="当前会话有多个待确认指令，请说明要确认哪一条。",
                utterance="",
                session_id=session_id,
                candidates=[p.token for p in pendings],
            )
        return pendings[0]

    def _stamp(self, decision: Decision, intent: Intent) -> Decision:
        """响应里也要带上解析器来源与"是否被纠偏"，否则留痕和对外的答复会不一致。"""
        decision.nlu_provider = intent.provider
        decision.degraded = bool(decision.degraded or intent.degraded)
        return decision

    def _log(self, trace_id: str, session_id: str, **kwargs) -> int:
        return self.audit.append(
            AuditRecord(
                trace_id=trace_id,
                session_id=session_id,
                ts=Store.now(),
                stage=kwargs.pop("stage"),
                **kwargs,
            )
        )

    def _terminal(
        self,
        kind: DecisionKind,
        trace_id: str,
        session_id: str,
        utterance: str,
        message: str,
        *,
        stage: str,
        started: float,
        intent: str = "",
        steps: list[ResolvedStep] | None = None,
        candidates: list[str] | None = None,
        confirmation_state: ConfirmationState | None = None,
    ) -> Decision:
        self._log(
            trace_id,
            session_id,
            stage=stage,
            utterance=utterance,
            intent=intent,
            confirmation_state=(confirmation_state or ConfirmationState.NOT_REQUIRED).value,
            risk_level=steps[0].risk_level.value if steps else "",
            risk_class=steps[0].risk_class if steps else "",
            tool=steps[0].tool if steps else "",
            targets=[t for s in (steps or []) for t in s.targets],
            error=message if kind is DecisionKind.REJECTED else "",
            detail={"candidates": candidates or [], "reason": message},
        )
        return Decision(
            trace_id=trace_id,
            kind=kind,
            message=message,
            utterance=utterance,
            session_id=session_id,
            intent=intent,
            steps=steps or [],
            candidates=candidates or [],
            risk_level=combine(*(s.risk_level for s in steps)) if steps else RiskLevel.LOW,
            dispatch_count=0,
            timings_ms={"total": round((time.perf_counter() - started) * 1000.0, 3)},
        )

    def _reject(
        self,
        trace_id: str,
        session_id: str,
        utterance: str,
        message: str,
        *,
        started: float,
        intent: str = "",
        steps: list[ResolvedStep] | None = None,
        confirmation_state: ConfirmationState | None = None,
    ) -> Decision:
        return self._terminal(
            DecisionKind.REJECTED,
            trace_id,
            session_id,
            utterance,
            message,
            stage="reject",
            started=started,
            intent=intent,
            steps=steps,
            confirmation_state=confirmation_state,
        )

    def _clarify(
        self,
        trace_id: str,
        session_id: str,
        utterance: str,
        message: str,
        candidates: list[str],
        *,
        steps: list[ResolvedStep],
        started: float,
    ) -> Decision:
        return self._terminal(
            DecisionKind.CLARIFICATION_REQUIRED,
            trace_id,
            session_id,
            utterance,
            message,
            stage="clarify",
            started=started,
            steps=steps,
            candidates=candidates,
        )


def action_utterance(pending) -> str:
    return getattr(pending, "utterance", "") or ""


def _first_risk(steps: list[ResolvedStep], level: RiskLevel) -> ResolvedStep | None:
    for step in steps:
        if step.risk_level is level:
            return step
    return None
