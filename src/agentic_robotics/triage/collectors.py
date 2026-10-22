"""Collectors turn AIR's existing signals into triage signals (ADR 0009).

Live:
- :class:`RuntimeTriageSink`: a ``Trace`` sink for runtime outcomes (unresolved, failed,
  cancels whose stop was not confirmed, adapter and interrupt errors, revocation during
  execution, admission rejections).
- :func:`resource_listener`: a ``ResourceManager.on_change`` listener for leases that
  expired while the resource may still be occupied.

Sweeps (:func:`sweep`), idempotent because each signal names its evidence:
- pending proposals, denied approvals, model refusals and sandbox refusals (work ledger)
- invalidated calibration (maintenance ledger)
- ledger integrity, requirement-evidence problems and source-catalog problems (checks)

After collecting, the sweep auto-resolves cleared conditions and escalates overdue items.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..provenance.project import Project
from .book import IngestResult, TriageBook
from .contracts import Signal, TriageItem
from .policy import fingerprint

__requirements__ = ("REQ14", "REQ15", "REQ17", "G11")

RUNTIME = "runtime"


class RuntimeTriageSink:
    """Add to a runtime's trace: ``runtime.trace.add_sink(RuntimeTriageSink(book))``."""

    def __init__(self, book: TriageBook) -> None:
        self.book = book
        self._commands: dict[str, tuple[str | None, str | None]] = {}

    def __call__(self, event: Any) -> None:
        signal = self.signal(event)
        if signal is not None:
            self.book.ingest(signal)

    def signal(self, event: Any) -> Signal | None:
        d: dict[str, Any] = dict(event.data)
        if event.kind == "submitted":
            self._commands[event.subject] = (d.get("robot_id"), d.get("skill_id"))
            return None
        robot, skill = self._commands.get(event.subject, (None, None))
        robot = d.get("robot_id") or robot
        skill = d.get("skill_id") or skill
        reason = str(d.get("reason") or "")
        labels = {k: str(v) for k, v in (("skill_id", skill), ("reason", reason)) if v}
        evidence = [f"command:{event.subject}"]
        if event.trace_id:
            evidence.append(f"trace:{event.trace_id}")

        def make(kind: str, title: str, **extra: str) -> Signal:
            return Signal(kind=kind, subject=event.subject, source=RUNTIME, title=title,
                          labels=labels | extra, robot_id=robot, evidence=tuple(evidence),
                          observed_at=None)

        what = f"{skill or 'command'} on {robot or 'unknown robot'}"
        if event.kind == "unresolved":
            return make("command.unresolved", f"outcome unknown: {what} ({reason})")
        if event.kind == "failed":
            return make("command.failed", f"failed: {what} ({reason})")
        if event.kind == "cancelled":
            confirmed = d.get("stopped_confirmed")
            if confirmed is True or reason == "cancelled_before_start":
                return None
            return make("command.cancelled", f"cancelled, stop not confirmed: {what}",
                        stopped_confirmed=str(confirmed).lower())
        if event.kind == "rejected":
            return make("command.rejected", f"admission rejected {what}: {reason}")
        if event.kind == "adapter_error":
            return make("command.adapter_error", f"adapter error on {robot or '?'}: "
                        f"{str(d.get('error', ''))[:120]}")
        if event.kind == "interrupt_failed":
            return make("command.interrupt_failed", f"interrupt failed: {what}")
        if event.kind == "authority_revoked_during_execution":
            return make("command.authority_revoked_during_execution",
                        f"authority revoked while running {what}")
        return None


def resource_listener(book: TriageBook) -> Callable[[Any], None]:
    """``resources.on_change(resource_listener(book))``."""

    def listen(reservation: Any) -> None:
        if getattr(reservation.state, "value", reservation.state) != "expired_occupied":
            return
        book.ingest(Signal(
            kind="resource.expired_occupied", subject=reservation.resource, source=RUNTIME,
            title=f"lease on {reservation.resource} expired while it may be occupied "
                  f"(holder {reservation.holder})",
            labels={"holder": reservation.holder,
                    "occupancy": getattr(reservation.physical_occupancy, "value", "")},
            evidence=(f"reservation:{reservation.reservation_id}",)))

    return listen


# -- sweeps -----------------------------------------------------------------------------
def _ledger_signals(project: Project) -> Iterable[Signal]:
    for e in project.ledger:
        if e.kind.startswith("triage."):
            continue
        ev = (f"ledger:{e.seq}",)
        d = e.data
        if e.kind == "work.approval" and d.get("approved") is False:
            yield Signal("work.approval_denied", e.subject, "work",
                         f"approval denied for {d.get('tool')} ({d.get('side_effect')}): "
                         f"{d.get('reason', '')}",
                         labels={"tool": str(d.get("tool")),
                                 "side_effect": str(d.get("side_effect"))},
                         evidence=ev, observed_at=e.ts)
        elif e.kind == "work.model_turn" and d.get("refusal_category"):
            yield Signal("work.model_refused", e.subject, "work",
                         f"model declined (category {d['refusal_category']})",
                         labels={"category": str(d["refusal_category"]),
                                 "model": str(d.get("model", ""))},
                         evidence=ev, observed_at=e.ts)
        elif e.kind == "activity.finished":
            act = d.get("activity", {})
            error = str(act.get("error") or "")
            if error.startswith("sandbox refused"):
                yield Signal("work.sandbox_refused", str(act.get("title", e.subject)), "work",
                             f"{act.get('title', '')}: {error}",
                             requirements=tuple(act.get("requirements", ())),
                             evidence=ev, observed_at=e.ts)
        elif e.kind == "calibration.invalidated":
            yield Signal("calibration.invalidated", e.subject, "maintenance",
                         f"calibration invalidated: {e.subject} "
                         f"({d.get('reason', 'component replaced')})",
                         robot_id=d.get("robot_id"), evidence=ev, observed_at=e.ts)


def _proposal_signals(project: Project) -> list[Signal]:
    from ..work.agent import proposals
    from ..work.contracts import ProposalStatus

    created = {e.subject: e for e in project.ledger.query(kind="work.proposed")}
    out = []
    for p in proposals(project, ProposalStatus.PROPOSED):
        entry = created.get(p.proposal_id)
        out.append(Signal("work.proposal_pending", p.proposal_id, "work.proposals",
                          f"proposal awaiting a decision: [{p.kind.value}] {p.title}",
                          labels={"proposal_kind": p.kind.value, "task_id": p.task_id},
                          requirements=p.requirements,
                          evidence=(f"proposal:{p.proposal_id}",),
                          observed_at=entry.ts if entry else None))
    return out


def _ledger_check(project: Project) -> list[Signal]:
    r = project.ledger.verify()
    if r.ok:
        return []
    return [Signal("ledger.integrity", "ledger", "checks.ledger",
                   f"ledger integrity failure at entry {r.first_bad_seq}: {r.problem}",
                   labels={"problem": r.problem},
                   evidence=(f"ledger-verify:{r.first_bad_seq}:{r.problem}",))]


def _requirements_check(project: Project, evidence_path: Path | None,
                        sources: Iterable[Path]) -> list[Signal] | None:
    from ..provenance.traceability import TestEvidence, build_matrix, check, scan_code

    reqs = project.requirements
    path = evidence_path or project.air_dir / "test-evidence.json"
    if reqs is None or not path.exists():
        return None                       # nothing to check: leave existing items alone
    code = scan_code(list(sources), project.root)
    evidence = TestEvidence.load(path)
    rows = build_matrix(reqs, code, evidence)
    out = []
    for problem in check(rows, reqs, code, evidence):
        subject = problem.split(":", 1)[0].strip()
        out.append(Signal("requirements.check", subject, "checks.requirements", problem,
                          requirements=(subject,) if reqs.get(subject) else (),
                          evidence=(f"check:{problem}",)))
    return out


def _sources_check(project: Project) -> list[Signal] | None:
    from ..provenance.sources import SourceCatalog, problems

    path = project.root / "docs" / "research" / "catalog.json"
    if not path.exists():
        return None
    known = [r.req_id for r in project.requirements.requirements] \
        if project.requirements else []
    return [Signal("sources.problem", p.split(":", 1)[0], "checks.sources", p,
                   evidence=(f"check:{p}",))
            for p in problems(SourceCatalog.load(path), known)]


@dataclass
class SweepReport:
    actions: Counter[str] = field(default_factory=Counter)
    auto_resolved: list[TriageItem] = field(default_factory=list)
    escalated: list[TriageItem] = field(default_factory=list)
    woken: list[TriageItem] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def summary(self) -> str:
        acts = ", ".join(f"{k} {v}" for k, v in sorted(self.actions.items())) or "no signals"
        return (f"{acts}; auto-resolved {len(self.auto_resolved)}, "
                f"escalated {len(self.escalated)}, woken {len(self.woken)}"
                + (f"; skipped {', '.join(self.skipped)}" if self.skipped else ""))


ALL_CHECKS = ("ledger", "work", "proposals", "requirements", "sources")


def sweep(book: TriageBook, *, checks: Iterable[str] = ALL_CHECKS,
          evidence_path: Path | None = None,
          sources: Iterable[Path] | None = None) -> SweepReport:
    project = book.project
    report = SweepReport()
    wanted = set(checks)

    def take(signals: Iterable[Signal]) -> None:
        for s in signals:
            res: IngestResult = book.ingest(s)
            report.actions[res.action] += 1

    def condition(source: str, signals: list[Signal] | None, name: str) -> None:
        if signals is None:                      # the check could not run: stale, not fixed
            report.skipped.append(name)
            book.mark_stale(source, True)
            return
        book.mark_stale(source, False)
        take(signals)
        present = {fingerprint(s, book.policy.route(s).group_by) for s in signals}
        report.auto_resolved += book.auto_resolve(source, present)

    if "ledger" in wanted:
        take(_ledger_check(project))
    if "work" in wanted:
        take(list(_ledger_signals(project)))
    if "proposals" in wanted:
        condition("work.proposals", _proposal_signals(project), "proposals")
    if "requirements" in wanted:
        roots = list(sources) if sources is not None else [project.root / "src"]
        condition("checks.requirements",
                  _requirements_check(project, evidence_path, roots), "requirements")
    if "sources" in wanted:
        condition("checks.sources", _sources_check(project), "sources")
    report.woken = book.wake_snoozed()
    report.escalated = book.escalate_overdue()
    return report
