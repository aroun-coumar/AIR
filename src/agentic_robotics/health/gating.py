"""Report vs gate: health restricts the capability report the runtime admits against.

Health never commands anything. It only *downgrades* capability profiles (AVAILABLE to
RESTRICTED or UNAVAILABLE) so admission refuses work the robot cannot do safely now,
and it refuses commands for recovery the robot owns (Microduck's ``limp_fall`` runs on
the robot; AIR waits for observations that show it healthy again).

    gate = HealthGate(trace=runtime.trace)
    gate.attach_health(health_manifest, aggregator)
    runtime = Runtime(skills, checks=gated_checks(gate, recovery=[health_manifest]))

``gated_checks`` inserts the gate immediately before ``check_capability``, so the
built-in capability check sees the gated report.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..contracts.capability import CapabilityProfile, CapabilityReport, ProfileStatus
from ..core.errors import AdmissionRejected
from ..runtime.admission import DEFAULT_CHECKS, AdmissionCheck, AdmissionContext, check_capability
from .aggregate import HealthAggregator
from .model import HealthLevel, HealthManifest, HealthReport

__requirements__ = ("REQ13", "REQ14", "G10", "G11")

_STATUS_RANK = {ProfileStatus.AVAILABLE: 0, ProfileStatus.RESTRICTED: 1,
                ProfileStatus.UNAVAILABLE: 2}

#: ``(report, now) -> report``: one source of downgrades (health, prognostics, ...).
Gater = Callable[[CapabilityReport, float], CapabilityReport]


def worse(a: ProfileStatus, b: ProfileStatus) -> ProfileStatus:
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


def downgrade(report: CapabilityReport,
              changes: Mapping[str, tuple[ProfileStatus, Iterable[str], Iterable[str]]]
              ) -> CapabilityReport:
    """Apply ``{capability or "*": (status, limiting_factors, evidence)}``; only ever makes
    a profile worse, and appends the factors and evidence that made it so."""
    if not changes:
        return report
    profiles = []
    for p in report.profiles:
        status, factors, evidence = p.status, list(p.limiting_factors), list(p.evidence)
        for key in (p.name, "*"):
            if key not in changes:
                continue
            new, fs, ev = changes[key]
            if _STATUS_RANK[new] > 0:
                status = worse(status, new)
                factors += [f for f in fs if f not in factors]
                evidence += [e for e in ev if e not in evidence]
        profiles.append(CapabilityProfile(p.name, status, dict(p.limits), tuple(factors),
                                          tuple(evidence)))
    return report.evolve(profiles=tuple(profiles))


def gate_capability(report: CapabilityReport, health: HealthReport,
                    manifest: HealthManifest) -> CapabilityReport:
    """The capability report after health gates are applied (pure)."""
    changes: dict[str, tuple[ProfileStatus, list[str], list[str]]] = {}
    statuses = (*health.items, *health.other)
    for gate in manifest.gates:
        hits = [s for s in statuses if gate.matches(s) and s.level is not HealthLevel.OK]
        if not hits:
            continue
        status = ProfileStatus.AVAILABLE
        for s in hits:
            status = worse(status, gate.status_for(s.level))
        if status is ProfileStatus.AVAILABLE:
            continue
        old = changes.get(gate.capability, (ProfileStatus.AVAILABLE, [], []))
        factors = old[1] + [f"health:{s.name}:{s.level.value}" for s in hits
                            if gate.status_for(s.level) is not ProfileStatus.AVAILABLE]
        evidence = old[2] + [f"health:{health.robot_id}@{health.observed_at:g}"]
        changes[gate.capability] = (worse(old[0], status), factors, evidence)
    return downgrade(report, changes)


class HealthGate:
    """An admission check that replaces ``ctx.capability`` with the gated report.

    The gated revision is the adapter's revision plus the number of times the gating
    outcome changed, so it is monotonic and a command planned against an older gated
    revision is refused by ``check_capability``."""

    def __init__(self, trace: Any = None) -> None:
        self.trace = trace
        self._gaters: dict[str, list[Gater]] = {}
        self._epoch: dict[str, int] = {}
        self._last: dict[str, tuple[Any, ...]] = {}

    def attach(self, robot_id: str, gater: Gater) -> None:
        self._gaters.setdefault(robot_id, []).append(gater)

    def attach_health(self, manifest: HealthManifest,
                      source: HealthAggregator | Callable[[float], HealthReport]) -> None:
        report_fn = source.report if isinstance(source, HealthAggregator) else source

        def gater(report: CapabilityReport, now: float) -> CapabilityReport:
            return gate_capability(report, report_fn(now), manifest)

        self.attach(manifest.robot_id, gater)

    def report(self, base: CapabilityReport, now: float) -> CapabilityReport:
        gated = base
        for gater in self._gaters.get(base.robot_id, ()):
            gated = gater(gated, now)
        before = {p.name: (p.status, p.limiting_factors) for p in base.profiles}
        key = tuple((p.name, p.status.value, p.limiting_factors) for p in gated.profiles
                    if before.get(p.name) != (p.status, p.limiting_factors))
        robot = base.robot_id
        if robot in self._last and self._last[robot] != key:
            self._epoch[robot] = self._epoch.get(robot, 0) + 1
            if self.trace is not None:
                self.trace.record("capability_gated", robot,
                                  profiles={p.name: p.status.value for p in gated.profiles},
                                  limiting_factors={p.name: list(p.limiting_factors)
                                                    for p in gated.profiles
                                                    if p.limiting_factors})
        self._last[robot] = key
        return replace(gated, capability_revision=base.capability_revision
                       + self._epoch.get(robot, 0))

    def __call__(self, ctx: AdmissionContext) -> None:
        ctx.capability = self.report(ctx.capability, ctx.clock.now())


class RobotOwnedRecoveryCheck:
    """Refuses commands for skills whose recovery the robot owns."""

    def __init__(self, manifests: Iterable[HealthManifest]) -> None:
        self.owned = {m.robot_id: frozenset(m.robot_owned_recovery) for m in manifests}

    def __call__(self, ctx: AdmissionContext) -> None:
        if ctx.command.skill_id in self.owned.get(ctx.command.robot_id, frozenset()):
            raise AdmissionRejected(
                "robot_owned_recovery", "this recovery is owned by the robot; AIR does not "
                "command it and waits for observations instead",
                robot_id=ctx.command.robot_id, skill_id=ctx.command.skill_id)


def gated_checks(gate: HealthGate, *, base: Sequence[AdmissionCheck] = DEFAULT_CHECKS,
                 recovery: Iterable[HealthManifest] = ()) -> tuple[AdmissionCheck, ...]:
    """``base`` with the gate placed right before ``check_capability`` (appended if the
    base has none) and, when manifests are given, the robot-owned-recovery refusal first."""
    out: list[AdmissionCheck] = []
    manifests = list(recovery)
    if manifests:
        out.append(RobotOwnedRecoveryCheck(manifests))
    placed = False
    for check in base:
        if check is check_capability and not placed:
            out.append(gate)
            placed = True
        out.append(check)
    if not placed:
        out.append(gate)
    return tuple(out)
