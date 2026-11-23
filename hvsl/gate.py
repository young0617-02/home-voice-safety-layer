"""F-007 confirmation gate: dangerous plans are parked here and never dispatched from them.

The gate is the only way a dangerous plan reaches the dispatcher, and
:meth:`take` is single-use: a token cannot fund two dispatches.

P0-5 adds two properties that only matter on a real Home Assistant box:

* **survives a restart** - when a :class:`~hvsl.store.Store` is injected every
  mutation is mirrored into the ``pending`` table, and :meth:`restore` reloads
  the unexpired rows after a restart. Unconfirmed plans are never silently lost;
* **expiry is auditable** - :meth:`expire_due` writes one ``stage=confirm`` /
  ``confirmation_state=expired`` row per dropped action (``error="ttl_expired"``),
  so a plan that silently timed out is visible in the trail.

Repeated failed confirmations (unknown token, wrong session, expired, reuse) are
counted; after ``max_failed_attempts`` in a row the gate refuses all attempts for
``failure_cooldown_seconds`` to slow down token guessing.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterable

from .audit import AuditLog
from .config import AuditRecord
from .errors import ConfirmationExpiredError
from .models import ConfirmationState, PendingAction, ResolvedStep, RiskLevel, token_hash
from .store import Store

DEFAULT_MAX_FAILED_ATTEMPTS = 5
DEFAULT_FAILURE_COOLDOWN_SECONDS = 60.0


class ConfirmationGate:
    """Holds :class:`PendingAction` objects until the user confirms or they expire.

    ``store=None`` keeps the historical pure-memory behaviour (all unit tests that
    do not care about persistence stay valid). ``audit=None`` simply means expiry is
    not recorded - production wires the engine's audit log in.
    """

    def __init__(
        self,
        ttl_seconds: float = 120.0,
        max_pending: int = 8,
        *,
        store: Store | None = None,
        audit: AuditLog | None = None,
        max_failed_attempts: int = DEFAULT_MAX_FAILED_ATTEMPTS,
        failure_cooldown_seconds: float = DEFAULT_FAILURE_COOLDOWN_SECONDS,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_pending = max_pending
        self.store = store
        self.audit = audit
        self.max_failed_attempts = max_failed_attempts
        self.failure_cooldown_seconds = failure_cooldown_seconds
        self._pending: dict[str, PendingAction] = {}
        self._failed_attempts = 0
        self._cooldown_until = 0.0

    # ------------------------------------------------------------------ mutations

    def park(
        self,
        *,
        session_id: str,
        utterance: str,
        steps: list[ResolvedStep],
        risk_class: str,
        trace_id: str = "",
        now: float | None = None,
    ) -> PendingAction:
        now = time.time() if now is None else now
        self.expire_due(now=now)
        if len(self._pending) >= self.max_pending:
            oldest = min(self._pending.values(), key=lambda p: p.created_at)
            # drop() also removes the persisted row; an evicted plan must not come
            # back from the database after a restart.
            self.drop(oldest.token)
        action = PendingAction(
            token=secrets.token_hex(5),
            session_id=session_id,
            utterance=utterance,
            steps=steps,
            risk_class=risk_class,
            created_at=now,
            expires_at=now + self.ttl_seconds,
            trace_id=trace_id,
        )
        self._pending[action.token] = action
        self._persist(action)
        return action

    def take(self, token: str, *, session_id: str | None = None, now: float | None = None) -> PendingAction:
        now = time.time() if now is None else now
        self._check_cooldown(now)
        action = self._pending.get(token)
        if action is None:
            self._register_failure(now)
            raise KeyError(f"confirmation token {token!r} is not pending")
        if session_id is not None and action.session_id != session_id:
            self._register_failure(now)
            raise PermissionError("confirmation token belongs to another session")
        if action.expires_at <= now:
            self._pending.pop(token, None)
            self._delete(token)
            self._register_failure(now)
            raise ConfirmationExpiredError(
                f"confirmation window ({self.ttl_seconds:.0f}s) expired before the user answered"
            )
        if action.consumed:
            self._register_failure(now)
            raise ValueError("this confirmation was already used")
        action.consumed = True
        self._pending.pop(token, None)
        self._delete(token)
        # A successful confirmation proves a legitimate user; reset the counter.
        self._failed_attempts = 0
        return action

    def drop(self, token: str) -> PendingAction | None:
        action = self._pending.pop(token, None)
        self._delete(token)
        return action

    def expire_due(self, *, now: float | None = None) -> list[PendingAction]:
        """Drop expired plans and leave one audit row per dropped plan."""
        now = time.time() if now is None else now
        due = [action for action in self._pending.values() if action.expires_at <= now]
        for action in due:
            self._pending.pop(action.token, None)
            self._delete(action.token)
            self._audit_expired(action)
        return due

    def clear(self) -> None:
        self._pending.clear()
        if self.store is not None:
            self.store.execute("DELETE FROM pending")

    # ------------------------------------------------------------------ reads

    def get(self, token: str) -> PendingAction | None:
        return self._pending.get(token)

    def for_session(self, session_id: str, *, now: float | None = None) -> list[PendingAction]:
        now = time.time() if now is None else now
        return [
            action
            for action in self._pending.values()
            if action.session_id == session_id and not action.consumed and action.expires_at > now
        ]

    def all(self, *, now: float | None = None) -> list[PendingAction]:
        """Every unexpired, unconsumed pending action across all sessions (P0-4)."""
        now = time.time() if now is None else now
        return sorted(
            (
                action
                for action in self._pending.values()
                if not action.consumed and action.expires_at > now
            ),
            key=lambda action: action.created_at,
        )

    def restore(self, *, now: float | None = None) -> int:
        """Reload unexpired persisted plans. Returns how many were restored.

        Expired rows are deleted without an audit row here: they already got one
        when they expired in the previous process (or were never announced to the
        user); re-emitting them on every boot would duplicate the trail.
        """
        if self.store is None:
            return 0
        now = time.time() if now is None else now
        rows = self.store.query(
            "SELECT * FROM pending WHERE expires_at > ? ORDER BY created_at ASC", (now,)
        )
        restored = 0
        for row in rows:
            token = str(row["token"])
            if token in self._pending:
                continue
            try:
                steps = [ResolvedStep.model_validate(item) for item in json.loads(row["steps"] or "[]")]
            except (TypeError, ValueError):
                # 损坏行不能进入确认队列：删掉，避免反复失败。
                self._delete(token)
                continue
            self._pending[token] = PendingAction(
                token=token,
                session_id=str(row["session_id"]),
                utterance=str(row["utterance"]),
                steps=steps,
                risk_class=str(row["risk_class"]),
                created_at=float(row["created_at"]),
                expires_at=float(row["expires_at"]),
                trace_id=str(row["trace_id"]),
            )
            restored += 1
        self.store.execute("DELETE FROM pending WHERE expires_at <= ?", (now,))
        return restored

    def __len__(self) -> int:
        return len(self._pending)

    def all_tokens(self) -> Iterable[str]:
        return list(self._pending)

    # ------------------------------------------------------------------ internals

    def _persist(self, action: PendingAction) -> None:
        if self.store is None:
            return
        self.store.execute(
            "INSERT OR REPLACE INTO pending"
            " (token, session_id, utterance, steps, risk_class, created_at, expires_at, trace_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                action.token,
                action.session_id,
                action.utterance,
                self.store.dumps([step.model_dump(mode="json") for step in action.steps]),
                action.risk_class,
                action.created_at,
                action.expires_at,
                action.trace_id,
            ),
        )

    def _delete(self, token: str) -> None:
        if self.store is not None:
            self.store.execute("DELETE FROM pending WHERE token = ?", (token,))

    def _audit_expired(self, action: PendingAction) -> None:
        if self.audit is None:
            return
        self.audit.append(
            AuditRecord(
                trace_id=action.trace_id or "",
                session_id=action.session_id,
                ts=Store.now(),
                stage="confirm",
                utterance=action.utterance,
                risk_level=RiskLevel.CONFIRM.value,
                risk_class=action.risk_class,
                confirmation_state=ConfirmationState.EXPIRED.value,
                tool=action.steps[0].tool if action.steps else "",
                targets=[target for step in action.steps for target in step.targets],
                idempotency_key="",
                detail={"pending_token_hash": token_hash(action.token), "expired": True},
                error="ttl_expired",
            )
        )

    def _check_cooldown(self, now: float) -> None:
        if now < self._cooldown_until:
            remaining = self._cooldown_until - now
            raise PermissionError(
                f"确认失败次数过多，已进入冷却，请 {remaining:.0f}s 后再试"
            )

    def _register_failure(self, now: float) -> None:
        if self.max_failed_attempts <= 0:
            return
        self._failed_attempts += 1
        if self._failed_attempts >= self.max_failed_attempts:
            self._cooldown_until = now + self.failure_cooldown_seconds
            self._failed_attempts = 0