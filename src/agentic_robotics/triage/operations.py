"""Operations collector: health, readiness, incidents and prognostics into triage (ADR 0015).

    sink = OperationsTriage(TriageBook(project, policy=operations_policy(load_policy(project))))
    sink.health(report)            # ERROR/STALE items; cleared conditions auto-resolve
    sink.readiness(ready_report)   # blocked modes
    sink.incident(incident)        # novel incidents, handoffs
    sink.prognostics(estimates, lead_s=7 * 86400)   # p5 inside the lead time

Each signal names its evidence (the report time, the incident step, the estimate id), so
feeding the same report twice is idempotent. :data:`OPERATIONS_RULES` route these kinds;
:func:`operations_policy` puts them in front of any policy (the built-in default routes
unknown kinds to quality/P3).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from .book import IngestResult, TriageBook
from .contracts import Priority, Severity, Signal
from .policy import TriagePolicy, TriageRule, fingerprint, m

__requirements__ = ("REQ14", "REQ15", "REQ16", "G11", "G13")

_MIN, _HOUR, _DAY = 60.0, 3600.0, 86400.0
_ESC = ("role:shift-lead", "role:safety-officer")

OPERATIONS_RULES: tuple[TriageRule, ...] = (
    TriageRule("health-critical", Severity.SAFETY, Priority.P1,
               kinds=("health.error", "health.stale"), matchers=(m("critical=true"),),
               owner="role:operator", escalation=_ESC, ack_within_s=5 * _MIN,
               resolve_within_s=1 * _HOUR, group_by=("kind", "robot_id", "item")),
    TriageRule("health-error", Severity.MISSION, Priority.P2, kinds=("health.error",),
               owner="role:operator", escalation=_ESC[:1], ack_within_s=30 * _MIN,
               group_by=("kind", "robot_id", "item"), auto_resolve=True),
    TriageRule("health-stale", Severity.MISSION, Priority.P2, kinds=("health.stale",),
               owner="role:integrator", escalation=_ESC[:1], ack_within_s=30 * _MIN,
               group_by=("kind", "robot_id", "item"), auto_resolve=True),
    TriageRule("readiness-failsafe", Severity.SAFETY, Priority.P1,
               kinds=("readiness.failsafe",), owner="role:operator", escalation=_ESC,
               ack_within_s=5 * _MIN, group_by=("kind", "robot_id", "flag")),
    TriageRule("readiness-blocked", Severity.MISSION, Priority.P2, kinds=("readiness.blocked",),
               owner="role:operator", ack_within_s=1 * _HOUR,
               group_by=("kind", "robot_id", "check", "subject"), auto_resolve=True),
    TriageRule("incident-aborted", Severity.SAFETY, Priority.P1, kinds=("incident.handed_off",),
               matchers=(m("aborted=true"),), owner="role:operator", escalation=_ESC,
               ack_within_s=5 * _MIN),
    TriageRule("incident-handoff", Severity.MISSION, Priority.P1, kinds=("incident.handed_off",),
               owner="role:operator", escalation=_ESC[:1], ack_within_s=15 * _MIN),
    TriageRule("incident-novel", Severity.MISSION, Priority.P2, kinds=("incident.novel",),
               owner="role:maintainer", escalation=_ESC[:1], ack_within_s=4 * _HOUR),
    TriageRule("prognostic-reached", Severity.MISSION, Priority.P1,
               kinds=("prognostic.threshold",), matchers=(m("reached=true"),),
               owner="role:maintenance", ack_within_s=1 * _HOUR,
               group_by=("kind", "robot_id", "component_id", "event", "reached")),
    TriageRule("prognostic-threshold", Severity.QUALITY, Priority.P3,
               kinds=("prognostic.threshold",), owner="role:maintenance",
               ack_within_s=2 * _DAY,
               group_by=("kind", "robot_id", "component_id", "event", "reached"),
               auto_resolve=True),
)


def operations_policy(base: TriagePolicy | None = None) -> TriagePolicy:
    """``base`` (default: the built-in policy) with the operations rules in front; a rule
    the base already names is kept as the base defines it."""
    from .policy import default_policy

    base = base or default_policy()
    names = {r.name for r in base.rules}
    return dataclasses.replace(base, rules=tuple(
        r for r in OPERATIONS_RULES if r.name not in names) + base.rules)


# -- signal builders ----------------------------------------------------------------------
def health_signals(report: Any) -> list[Signal]:
    out = []
    for s in report.items:
        if not s.level.bad:
            continue
        kind = "health.stale" if s.level.value == "stale" else "health.error"
        out.append(Signal(
            kind, f"{report.robot_id}:{s.name}", f"health:{report.robot_id}",
            f"{s.level.value.upper()} {s.name} on {report.robot_id}: {s.message}",
            labels={"item": s.name, "path": s.path, "level": s.level.value,
                    "critical": str(s.critical).lower()},
            robot_id=report.robot_id, requirements=("REQ13", "REQ14"),
            evidence=(f"health:{report.robot_id}:{s.name}@{report.observed_at:g}",),
            observed_at=s.observed_at or report.observed_at))
    return out


def readiness_signals(report: Any) -> list[Signal]:
    out = []
    when = f"{report.evaluated_at:g}"
    for r in report.results:
        if not r.blocks:
            continue
        modes = ",".join(sorted(r.blocks))
        out.append(Signal(
            "readiness.blocked", f"{report.robot_id}:{r.check_id}:{r.subject}",
            f"readiness:{report.robot_id}",
            f"{report.robot_id} not ready ({modes}): {r.check_id}: {r.message}",
            labels={"check": r.check_id, "subject": r.subject, "modes": modes,
                    "level": r.level.value},
            robot_id=report.robot_id, requirements=("REQ13",),
            evidence=(f"readiness:{report.robot_id}:{r.check_id}:{r.subject}@{when}",),
            observed_at=report.evaluated_at))
    for flag in sorted(report.failsafe):
        out.append(Signal(
            "readiness.failsafe", f"{report.robot_id}:{flag}", f"readiness:{report.robot_id}",
            f"failsafe {flag} raised on {report.robot_id}", labels={"flag": flag},
            robot_id=report.robot_id, requirements=("REQ13", "REQ14"),
            evidence=(f"readiness:{report.robot_id}:failsafe:{flag}@{when}",),
            observed_at=report.evaluated_at))
    return out


def incident_signals(incident: Any) -> list[Signal]:
    out = []
    base = {"detector": incident.detector, "signal": incident.signal}
    if incident.novel:
        out.append(Signal(
            "incident.novel", incident.incident_id, "incidents",
            f"novel failure on {incident.robot_id}: {incident.summary}",
            labels=base, robot_id=incident.robot_id, requirements=("REQ14", "G11"),
            evidence=(f"incident:{incident.incident_id}",), observed_at=incident.detected_at))
    if incident.state.value == "handed_off":
        aborted = any(t.outcome.value == "aborted" for t in incident.tests) or any(
            r.endswith(":unresolved") for r in incident.recovery)
        out.append(Signal(
            "incident.handed_off", incident.incident_id, "incidents",
            f"incident handed to {incident.handed_off_to} on {incident.robot_id}: "
            f"{incident.summary}",
            labels={**base, "to": str(incident.handed_off_to),
                    "aborted": str(aborted).lower()},
            robot_id=incident.robot_id, requirements=("REQ14", "REQ17"),
            evidence=(f"incident:{incident.incident_id}:handoff",),
            observed_at=incident.detected_at))
    return out


def prognostic_signals(estimates: Iterable[Any], lead_s: float) -> list[Signal]:
    from ..maintenance.prognostics import threshold_crossings

    out = []
    for e in threshold_crossings(estimates, lead_s):
        p5, _ = e.quantile("p5")
        reached = e.reached()
        out.append(Signal(
            "prognostic.threshold", f"{e.robot_id}:{e.component_id}:{e.event}",
            f"prognostics:{e.robot_id}",
            f"{e.component_id} on {e.robot_id}: " + (f"{e.event} reached" if reached else
                                                    f"{e.event} within {p5 / 3600:.3g} h (p5)"),
            labels={"component_id": e.component_id, "event": e.event,
                    "reached": str(reached).lower()},
            robot_id=e.robot_id, requirements=("REQ16", "G13"),
            evidence=(f"prognostic:{e.estimate_id}",), observed_at=e.estimated_at))
    return out


class OperationsTriage:
    """Feeds operations results into a :class:`TriageBook`. Whole-report inputs (health,
    readiness, prognostics) also auto-resolve the items whose condition cleared."""

    def __init__(self, book: TriageBook) -> None:
        self.book = book

    def _take(self, signals: list[Signal], source: str | None) -> list[IngestResult]:
        results = [self.book.ingest(s) for s in signals]
        if source is not None:
            present = {fingerprint(s, self.book.policy.route(s).group_by) for s in signals}
            self.book.auto_resolve(source, present)
        return results

    def health(self, report: Any) -> list[IngestResult]:
        return self._take(health_signals(report), f"health:{report.robot_id}")

    def readiness(self, report: Any) -> list[IngestResult]:
        return self._take(readiness_signals(report), f"readiness:{report.robot_id}")

    def incident(self, incident: Any) -> list[IngestResult]:
        return self._take(incident_signals(incident), None)

    def prognostics(self, estimates: Iterable[Any], *, lead_s: float,
                    robot_id: str | None = None) -> list[IngestResult]:
        """With ``robot_id`` the estimates are that robot's complete set, so items for
        components no longer inside the lead time resolve."""
        ests = list(estimates)
        source = f"prognostics:{robot_id}" if robot_id else None
        return self._take(prognostic_signals(ests, lead_s), source)
