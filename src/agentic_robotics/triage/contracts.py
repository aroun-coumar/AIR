"""Triage contracts: signals in, tracked items out (ADR 0009).

Two separate axes, as in Bugzilla/Firefox (severity S1-S4 vs priority P1-P5) and the
Kubernetes triage labels:

- **Severity** is impact: what is at stake if this is real.
- **Priority** is urgency: how soon someone must act.

A :class:`Signal` is a raw observation from a collector (a runtime event, a failed check,
a pending proposal). Signals with the same fingerprint fold into one :class:`TriageItem`,
counting occurrences, the way Alertmanager and Sentry group alerts and events.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..core.schema import Contract, contract

__requirements__ = ("REQ14", "REQ15", "REQ17")


class Severity(StrEnum):
    SAFETY = "safety"        # people, robots or the environment could be harmed
    INTEGRITY = "integrity"  # evidence, authority or data can no longer be trusted
    MISSION = "mission"      # work fails or its outcome is unknown
    QUALITY = "quality"      # degraded, slower or more expensive than it should be
    HYGIENE = "hygiene"      # housekeeping: stale, noisy, inconsistent

    @property
    def rank(self) -> int:
        return list(Severity).index(self)

    @property
    def protected(self) -> bool:
        """Never silenced or inhibited; agents cannot resolve or dismiss it."""
        return self in (Severity.SAFETY, Severity.INTEGRITY)


class Priority(StrEnum):
    P1 = "P1"  # act now
    P2 = "P2"  # act this shift / today
    P3 = "P3"  # act this iteration
    P4 = "P4"  # backlog

    def raised(self) -> Priority:
        """One level more urgent (P1 stays P1)."""
        order = list(Priority)
        return order[max(order.index(self) - 1, 0)]

    @staticmethod
    def most_urgent(a: Priority, b: Priority) -> Priority:
        return min(a, b, key=lambda p: list(Priority).index(p))


class TriageState(StrEnum):
    NEW = "new"                  # nobody has looked at it yet
    TRIAGED = "triaged"          # acknowledged and classified
    ASSIGNED = "assigned"        # an owner is responsible
    IN_PROGRESS = "in_progress"  # someone is working on it
    ESCALATED = "escalated"      # a deadline passed; the next owner in the chain has it
    SNOOZED = "snoozed"          # deferred until a time or an occurrence count; never
                                 # for protected severities
    RESOLVED = "resolved"        # fixed, with a reason (and evidence for safety items)
    DISMISSED = "dismissed"      # not a problem / won't fix, with a reason
    REGRESSED = "regressed"      # was resolved, and the same fingerprint came back

    @property
    def open(self) -> bool:
        return self not in (TriageState.RESOLVED, TriageState.DISMISSED)

    @property
    def acknowledged(self) -> bool:
        return self not in (TriageState.NEW, TriageState.REGRESSED, TriageState.ESCALATED)


_WORKING = frozenset({TriageState.TRIAGED, TriageState.ASSIGNED, TriageState.IN_PROGRESS,
                      TriageState.RESOLVED, TriageState.DISMISSED, TriageState.ESCALATED,
                      TriageState.SNOOZED})


#: Allowed moves. Regression and escalation are made by the triage system itself.
TRANSITIONS: dict[TriageState, frozenset[TriageState]] = {
    TriageState.NEW: _WORKING,
    TriageState.TRIAGED: _WORKING - {TriageState.TRIAGED},
    TriageState.ASSIGNED: _WORKING - {TriageState.TRIAGED},
    TriageState.IN_PROGRESS: _WORKING - {TriageState.TRIAGED, TriageState.IN_PROGRESS},
    TriageState.ESCALATED: _WORKING,
    TriageState.REGRESSED: _WORKING,
    TriageState.SNOOZED: frozenset({TriageState.NEW, TriageState.TRIAGED,
                                    TriageState.ASSIGNED, TriageState.IN_PROGRESS,
                                    TriageState.RESOLVED, TriageState.DISMISSED}),
    TriageState.RESOLVED: frozenset({TriageState.REGRESSED}),
    TriageState.DISMISSED: frozenset({TriageState.NEW}),
}


@dataclass(frozen=True)
class Signal:
    """One observation from a collector. ``evidence`` holds references such as
    ``ledger:<seq>``, ``trace:<trace_id>`` or ``command:<id>``; a signal whose evidence
    is already on the item is not counted again, so sweeps are idempotent."""

    kind: str
    subject: str
    source: str
    title: str
    labels: dict[str, str] = field(default_factory=dict)
    robot_id: str | None = None
    requirements: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    observed_at: float | None = None


@contract("triage_item", version="1.0")
class TriageItem(Contract):
    item_id: str
    fingerprint: str                     # SHA-256 hex over the rule's group_by labels
    kind: str
    source: str
    subject: str
    title: str
    severity: Severity
    priority: Priority
    state: TriageState = TriageState.NEW
    rule: str = "default"
    owner: str = "role:operator"
    assignee: str | None = None
    escalation: tuple[str, ...] = ()
    escalation_level: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    robot_id: str | None = None
    requirements: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    first_seen: float = 0.0
    last_seen: float = 0.0
    count: int = 1
    ack_due: float | None = None
    resolve_due: float | None = None
    resolve_escalated: bool = False
    suppressed_by: str | None = None     # silence or inhibition id, never on protected items
    resolution: str = ""
    grouping_version: int = 0            # fingerprint algorithm; 0 = early builds
    missing_group_labels: tuple[str, ...] = ()   # group_by labels the signal did not carry
    content_hash: str = ""               # last signal's title + labels: changes update item
    priority_locked: bool = False        # a person set the priority; rules don't change it
    snooze_until: float | None = None
    wake_after_count: int | None = None  # wake when the occurrence count reaches this
    stale: bool = False                  # its check could not run at the last sweep

    def overdue(self, now: float) -> bool:
        if not self.state.open or self.state is TriageState.SNOOZED:
            return False
        if not self.state.acknowledged and self.ack_due is not None and now > self.ack_due:
            return True
        return self.resolve_due is not None and now > self.resolve_due
