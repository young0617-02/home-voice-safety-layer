"""F-007/F-008 re-exported for Home Assistant.

The gate itself lives in the installable ``hvsl`` package so it can be tested and
evaluated without HA. HA-side code imports it from here; both refer to the same
objects, which is asserted by ``tests/test_integration_backend.py``.
"""

from __future__ import annotations

from hvsl.errors import ConfirmationExpiredError
from hvsl.gate import ConfirmationGate
from hvsl.idempotency import GuardResult, IdempotencyGuard
from hvsl.models import ConfirmationState, DecisionKind, PendingAction, RiskLevel
from hvsl.risk import RiskPolicy, combine
from hvsl.tools import ToolDef, ToolRegistry, build_default_registry

__all__ = [
    "ConfirmationExpiredError",
    "ConfirmationGate",
    "ConfirmationState",
    "DecisionKind",
    "GuardResult",
    "IdempotencyGuard",
    "PendingAction",
    "RiskLevel",
    "RiskPolicy",
    "ToolDef",
    "ToolRegistry",
    "build_default_registry",
    "combine",
]
