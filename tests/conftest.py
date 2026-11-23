from __future__ import annotations

import pytest

from hvsl.backends.simulated import SimulatedBackend
from hvsl.config import Settings
from hvsl.engine import DecisionEngine
from hvsl.models import Intent, PlannedCall
from hvsl.risk import RiskPolicy
from hvsl.store import Store
from hvsl.tools import build_default_registry

CATALOG = "devices/devices.json"
POLICY = "hvsl/policy/risk_policy.json"


class FixedParser:
    """Inject an exact plan so gate tests do not depend on the phrasing rules."""

    name = "fixed"

    def __init__(self, steps: list[PlannedCall] | None = None) -> None:
        self.steps = steps or []

    def parse(self, utterance: str) -> Intent:
        return Intent(utterance=utterance, steps=[s.model_copy(deep=True) for s in self.steps], provider=self.name)


def make_settings(**overrides) -> Settings:
    base = dict(
        db_path=":memory:",
        policy_path=POLICY,
        device_catalog=CATALOG,
        confirm_ttl_seconds=60.0,
        dedup_window_seconds=30.0,
        nlu_provider="rule",
    )
    base.update(overrides)
    return Settings(**base)


def make_engine(
    *,
    settings: Settings | None = None,
    backend=None,
    parser=None,
    registry=None,
    policy: RiskPolicy | None = None,
    store: Store | None = None,
) -> DecisionEngine:
    settings = settings or make_settings()
    registry = registry or build_default_registry()
    policy = policy or RiskPolicy.load(settings.policy_path)
    backend = backend or SimulatedBackend.from_catalog(settings.device_catalog)
    parser = parser or FixedParser()
    return DecisionEngine(
        backend,
        parser,
        settings=settings,
        registry=registry,
        policy=policy,
        store=store or Store(":memory:"),
    )


def step(tool: str, target: str, **args) -> PlannedCall:
    return PlannedCall(tool=tool, target=target, args=args)


@pytest.fixture()
def backend() -> SimulatedBackend:
    return SimulatedBackend.from_catalog(CATALOG)


@pytest.fixture()
def registry():
    return build_default_registry()


@pytest.fixture()
def policy() -> RiskPolicy:
    return RiskPolicy.load(POLICY)


@pytest.fixture()
def engine():
    built = make_engine()
    yield built
    built.store.close()


def collect_danger_probes(backend, registry, policy) -> list[dict]:
    """Every (tool, entity) pair the policy considers dangerous.

    This is what makes A1 an exhaustive check instead of a hand-picked one: add a
    danger rule and the probe set grows automatically.
    """
    probes: list[dict] = []
    for entity in backend.entities():
        for tool in registry.all():
            if tool.ha_service[0] != entity.domain:
                continue
            risk, risk_class, reason = policy.classify(tool.name, [entity])
            if risk.value == "low" or not risk_class:
                continue
            probes.append(
                {
                    "entity": entity,
                    "tool": tool,
                    "risk": risk,
                    "risk_class": risk_class,
                    "reason": reason,
                    "utterance": f"{tool.name} {entity.display_name}",
                }
            )
    return probes


@pytest.fixture()
def danger_probes(backend, registry, policy) -> list[dict]:
    return collect_danger_probes(backend, registry, policy)
