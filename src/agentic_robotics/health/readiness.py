"""Readiness gate: pluggable checks before activation (PX4 ``HealthAndArmingChecks``
pattern, re-implemented).

Each check reports results with a health level, the operating modes it blocks and any
failsafe flags it raises. The gate runs checks in a defined order (``order``, then
``check_id``), so reports are reproducible, and produces one :class:`ReadinessReport`
with ``can_run`` per mode and the failsafe flags::

    gate = ReadinessGate([HealthCheck(), CapabilityCheck({"autonomous": ("mobility",)})],
                         modes=("manual", "autonomous"))
    report = gate.evaluate(ReadinessContext(robot_id, now, health=..., capability=...))
    gate.require("autonomous", ctx)      # raises NotReady before activation

A check that raises is a failed check that blocks every mode: the gate fails closed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..contracts.capability import CapabilityReport, ProfileStatus
from ..contracts.observation import Observation
from ..core.errors import FrameworkError
from ..core.schema import Contract, contract
from .model import HealthLevel, HealthReport

__requirements__ = ("REQ13", "REQ14", "G10", "G11", "G13")

ALL_MODES = "*"


class NotReady(FrameworkError):
    code = "not_ready"


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    level: HealthLevel
    message: str
    subject: str = ""
    blocks: tuple[str, ...] = ()        # modes this result prevents ("*" = all)
    failsafe: tuple[str, ...] = ()      # failsafe flags it raises (e.g. "battery_critical")


@dataclass
class ReadinessContext:
    robot_id: str
    now: float
    health: HealthReport | None = None
    capability: CapabilityReport | None = None
    observations: Mapping[str, Observation] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class ReadinessCheck(Protocol):
    check_id: str
    order: int

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]: ...


@contract("readiness_report", version="1.0")
class ReadinessReport(Contract):
    robot_id: str
    evaluated_at: float
    modes: tuple[str, ...]
    can_run: dict[str, bool]
    failsafe: dict[str, bool] = field(default_factory=dict)
    level: HealthLevel = HealthLevel.OK
    results: tuple[CheckResult, ...] = ()
    checks_run: tuple[str, ...] = ()

    def ready(self, mode: str) -> bool:
        return bool(self.can_run.get(mode, False))

    def blocking(self, mode: str) -> list[CheckResult]:
        return [r for r in self.results if ALL_MODES in r.blocks or mode in r.blocks]


class ReadinessGate:
    def __init__(self, checks: Sequence[ReadinessCheck], modes: Sequence[str]) -> None:
        ids = [c.check_id for c in checks]
        if len(ids) != len(set(ids)):
            raise FrameworkError("duplicate readiness check ids", checks=ids)
        if not modes or ALL_MODES in modes:
            raise FrameworkError("modes must be named (and not '*')")
        self.checks = sorted(checks, key=lambda c: (c.order, c.check_id))
        self.modes = tuple(modes)

    def evaluate(self, ctx: ReadinessContext) -> ReadinessReport:
        results: list[CheckResult] = []
        for check in self.checks:
            try:
                results.extend(check.run(ctx))
            except Exception as exc:
                results.append(CheckResult(check.check_id, HealthLevel.ERROR,
                                           f"check crashed: {type(exc).__name__}: {exc}",
                                           blocks=(ALL_MODES,)))
        can_run = {m: not any(ALL_MODES in r.blocks or m in r.blocks for r in results)
                   for m in self.modes}
        flags = sorted({f for r in results for f in r.failsafe})
        return ReadinessReport(robot_id=ctx.robot_id, evaluated_at=ctx.now, modes=self.modes,
                               can_run=can_run, failsafe={f: True for f in flags},
                               level=HealthLevel.worst(r.level for r in results),
                               results=tuple(results),
                               checks_run=tuple(c.check_id for c in self.checks))

    def require(self, mode: str, ctx: ReadinessContext, project: Any = None
                ) -> ReadinessReport:
        """Evaluate before activating ``mode``; raise :class:`NotReady` if it cannot run.
        With a project, the evaluation is a ``readiness.evaluated`` ledger entry."""
        if mode not in self.modes:
            raise NotReady("unknown mode", mode=mode, modes=list(self.modes))
        report = self.evaluate(ctx)
        if project is not None:
            project.ledger.append("readiness.evaluated", ctx.robot_id,
                                  {"mode": mode, "ready": report.ready(mode),
                                   "report": report.to_dict()})
        if not report.ready(mode):
            raise NotReady("robot is not ready for this mode", robot_id=ctx.robot_id,
                           mode=mode, blocking=[f"{r.check_id}: {r.message}"
                                                for r in report.blocking(mode)])
        return report


# -- built-in checks ---------------------------------------------------------------------
@dataclass
class HealthCheck:
    """ERROR and STALE items block every mode (critical ones also raise a failsafe flag);
    WARN items are reported and block only ``warn_blocks`` modes."""

    check_id: str = "health"
    order: int = 10
    warn_blocks: tuple[str, ...] = ()

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]:
        if ctx.health is None:
            yield CheckResult(self.check_id, HealthLevel.STALE, "no health report",
                              blocks=(ALL_MODES,))
            return
        for s in (*ctx.health.items, *ctx.health.other):
            if s.level is HealthLevel.OK:
                continue
            if s.level is HealthLevel.STALE and s in ctx.health.other:
                continue
            blocks = (ALL_MODES,) if s.level.bad else self.warn_blocks
            failsafe = (f"health:{s.name}",) if s.level.bad and s.critical else ()
            yield CheckResult(self.check_id, s.level, s.message or s.level.value,
                              subject=s.name, blocks=blocks, failsafe=failsafe)


@dataclass
class CapabilityCheck:
    """Each mode needs its capabilities AVAILABLE (or RESTRICTED when ``allow_restricted``)
    in a report no older than ``max_age_s``."""

    required: dict[str, tuple[str, ...]]
    check_id: str = "capability"
    order: int = 20
    allow_restricted: bool = True
    max_age_s: float = 5.0

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]:
        report = ctx.capability
        if report is None or report.age(ctx.now) > self.max_age_s:
            yield CheckResult(self.check_id, HealthLevel.STALE,
                              "no capability report" if report is None else
                              f"capability report is {report.age(ctx.now):.3g} s old",
                              blocks=tuple(self.required))
            return
        for mode, caps in self.required.items():
            for cap in caps:
                p = report.get(cap)
                if p is None or p.status is ProfileStatus.UNAVAILABLE or (
                        p.status is ProfileStatus.RESTRICTED and not self.allow_restricted):
                    why = ", ".join(p.limiting_factors) if p else "not reported"
                    yield CheckResult(self.check_id, HealthLevel.ERROR,
                                      f"{cap} is {p.status.value if p else 'missing'} ({why})",
                                      subject=cap, blocks=(mode,))


@dataclass
class FreshnessCheck:
    """Signals each mode depends on must have a fresh, non-invalid observation."""

    required: dict[str, tuple[str, ...]]
    max_age_s: float = 1.0
    check_id: str = "freshness"
    order: int = 30

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]:
        for mode, signals in self.required.items():
            for sig in signals:
                obs = ctx.observations.get(sig)
                if obs is None:
                    yield CheckResult(self.check_id, HealthLevel.ERROR, f"no {sig}",
                                      subject=sig, blocks=(mode,))
                elif obs.age(ctx.now) > self.max_age_s:
                    yield CheckResult(self.check_id, HealthLevel.STALE,
                                      f"{sig} is {obs.age(ctx.now):.3g} s old",
                                      subject=sig, blocks=(mode,))
                elif obs.quality.value == "invalid":
                    yield CheckResult(self.check_id, HealthLevel.ERROR, f"{sig} is invalid",
                                      subject=sig, blocks=(mode,))


@dataclass
class ManifestCheck:
    """Hardware catalog data errors in the manifest block every mode (advice does not)."""

    manifest: Any
    catalog: Any
    check_id: str = "manifest"
    order: int = 0

    def run(self, ctx: ReadinessContext) -> Iterable[CheckResult]:
        from ..hardware.validate import Level, check_manifest

        for f in check_manifest(self.manifest, self.catalog):
            if f.level is Level.ERROR:
                yield CheckResult(self.check_id, HealthLevel.ERROR, f.message, subject=f.where,
                                  blocks=(ALL_MODES,))
