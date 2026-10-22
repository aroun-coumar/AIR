"""Triage: signals -> deduplicated items -> owners, deadlines, decisions (ADR 0009)."""

from pathlib import Path

import pytest

from agentic_robotics.cli import main
from agentic_robotics.contracts.resources import Occupancy
from agentic_robotics.core.clock import ManualClock
from agentic_robotics.core.errors import AuthorityError, SchemaError
from agentic_robotics.provenance import Project
from agentic_robotics.runtime.resources import ResourceManager
from agentic_robotics.runtime.trace import TraceEvent
from agentic_robotics.triage import (
    Matcher,
    Priority,
    RuntimeTriageSink,
    Severity,
    Signal,
    TriageBook,
    TriageError,
    TriagePolicy,
    TriageState,
    default_policy,
    resource_listener,
    sweep,
)
from agentic_robotics.work.agent import decide
from agentic_robotics.work.contracts import Proposal, ProposalKind

pytestmark = pytest.mark.req("REQ14", "REQ15", "REQ17")
escalation = pytest.mark.req("G11", "G12")


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    return Project.init(tmp_path, name="t")


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def book(project: Project, clock: Clock) -> TriageBook:
    return TriageBook(project, now=clock)


def sig(kind: str = "command.unresolved", subject: str = "cmd-1", robot: str = "arm-1",
        evidence: tuple[str, ...] = ("command:cmd-1",), **labels: str) -> Signal:
    return Signal(kind, subject, "runtime", f"{kind} {subject}", labels=labels,
                  robot_id=robot, evidence=evidence)


def test_routing_and_separate_severity_priority_axes(book: TriageBook) -> None:
    item = book.ingest(sig()).item
    assert (item.severity, item.priority, item.owner, item.rule) == \
        (Severity.MISSION, Priority.P1, "role:operator", "unresolved-outcome")
    assert item.ack_due == item.first_seen + 15 * 60
    noisy = book.ingest(sig("command.rejected", "cmd-9", reason="busy")).item
    assert (noisy.severity, noisy.priority) == (Severity.HYGIENE, Priority.P4)
    other = book.ingest(sig("some.new_kind", "x")).item
    assert other.rule == "default" and other.severity is Severity.QUALITY


def test_fingerprint_dedup_counts_and_is_idempotent(book: TriageBook) -> None:
    a = book.ingest(sig("command.rejected", "cmd-1", reason="busy",
                        evidence=("command:cmd-1",)))
    b = book.ingest(sig("command.rejected", "cmd-2", reason="busy",
                        evidence=("command:cmd-2",)))
    again = book.ingest(sig("command.rejected", "cmd-2", reason="busy",
                            evidence=("command:cmd-2",)))
    other_reason = book.ingest(sig("command.rejected", "cmd-3", reason="stale_input",
                                   evidence=("command:cmd-3",)))
    assert a.action == "opened" and b.action == "occurred" and again.action == "unchanged"
    assert b.item.item_id == a.item.item_id and b.item.count == 2
    assert other_reason.item.item_id != a.item.item_id


def test_lifecycle_and_invalid_transitions(book: TriageBook) -> None:
    i = book.ingest(sig()).item
    assert book.acknowledge(i.item_id, "operator:alice").state is TriageState.TRIAGED
    assert book.assign(i.item_id, "tech:bo", "operator:alice").assignee == "tech:bo"
    assert book.start(i.item_id, "tech:bo").state is TriageState.IN_PROGRESS
    done = book.resolve(i.item_id, "tech:bo", "reconciled: part is in the gripper",
                        ["ledger:12"])
    assert done.state is TriageState.RESOLVED and "ledger:12" in done.evidence
    with pytest.raises(TriageError):
        book.acknowledge(i.item_id, "operator:alice")
    with pytest.raises(TriageError):
        book.get("tri_nope")


def test_agents_cannot_close_protected_items_and_reasons_are_required(
        book: TriageBook) -> None:
    safety = book.ingest(sig("command.cancelled", stopped_confirmed="false")).item
    assert safety.severity is Severity.SAFETY
    with pytest.raises(AuthorityError):
        book.resolve(safety.item_id, "agent:work", "looks fine", ["ledger:1"])
    with pytest.raises(AuthorityError):
        book.dismiss(safety.item_id, "agent:work", "noise")
    with pytest.raises(TriageError):                 # safety needs evidence
        book.resolve(safety.item_id, "operator:alice", "checked")
    with pytest.raises(TriageError):                 # dismissal needs a reason
        book.dismiss(safety.item_id, "operator:alice", " ")
    with pytest.raises(AuthorityError):              # the system is not a decider
        book.acknowledge(safety.item_id, "system:triage")
    # Agents may still acknowledge and work protected items...
    assert book.acknowledge(safety.item_id, "agent:diagnoser").state is TriageState.TRIAGED
    # ...and may close unprotected ones.
    q = book.ingest(sig("work.model_refused", "act-1", category="cyber")).item
    assert book.resolve(q.item_id, "agent:work", "rephrased task").state is \
        TriageState.RESOLVED


def test_resolved_fingerprint_that_returns_is_regressed(book: TriageBook,
                                                         clock: Clock) -> None:
    i = book.ingest(sig("command.failed", "cmd-1", reason="postcondition_failed",
                        skill_id="pick", evidence=("command:cmd-1",))).item
    book.resolve(i.item_id, "operator:alice", "re-taught grasp")
    clock.t += 3600
    back = book.ingest(sig("command.failed", "cmd-7", reason="postcondition_failed",
                           skill_id="pick", evidence=("command:cmd-7",)))
    assert back.action == "regressed" and back.item.state is TriageState.REGRESSED
    assert back.item.ack_due == clock.t + 4 * 3600
    assert book.dismiss(i.item_id, "operator:alice", "known flaky fixture").state is \
        TriageState.DISMISSED
    assert book.reopen(i.item_id, "operator:alice", "seen on a new fixture").state is \
        TriageState.NEW


@escalation
def test_unacknowledged_items_escalate_along_the_chain(book: TriageBook,
                                                       clock: Clock) -> None:
    i = book.ingest(sig()).item                    # ack within 15 min
    assert book.escalate_overdue() == []
    clock.t += 16 * 60
    [e1] = book.escalate_overdue()
    assert (e1.state, e1.owner, e1.escalation_level) == \
        (TriageState.ESCALATED, "role:shift-lead", 1)
    clock.t += 16 * 60
    [e2] = book.escalate_overdue()
    assert (e2.owner, e2.escalation_level) == ("role:safety-officer", 2)
    book.acknowledge(i.item_id, "officer:kim")
    clock.t += 16 * 60
    assert book.escalate_overdue() == []           # acknowledged: no further escalation
    clock.t += 4 * 3600                             # resolve deadline (4 h) passes once
    [e3] = book.escalate_overdue()
    assert e3.resolve_escalated and "resolve deadline" in book.history(i.item_id)[-1]["reason"]
    assert [x.item_id for x in book.items(overdue=True)] == [i.item_id]


@escalation
def test_silences_never_hide_protected_items(book: TriageBook, clock: Clock) -> None:
    noisy = book.ingest(sig("command.rejected", "cmd-1", reason="busy")).item
    safety = book.ingest(sig("command.interrupt_failed", "cmd-2")).item
    with pytest.raises(AuthorityError):
        book.silence([Matcher("robot_id", "arm-1")], duration_s=600, by="agent:x",
                     comment="quiet")
    with pytest.raises(TriageError):
        book.silence([Matcher("robot_id", "arm-1")], duration_s=600, by="op:a", comment="")
    s = book.silence([Matcher("robot_id", "arm-1")], duration_s=600, by="op:a",
                     comment="arm under maintenance")
    visible = {i.item_id for i in book.items()}
    assert noisy.item_id not in visible and safety.item_id in visible
    assert book.get(noisy.item_id).suppressed_by == s.silence_id
    book.unsilence(s.silence_id, "op:a")
    assert book.get(noisy.item_id).suppressed_by is None
    s2 = book.silence([Matcher.parse("kind=~command\\..*")], duration_s=60, by="op:a",
                      comment="short")
    clock.t += 120
    book.refresh_suppression()
    assert s2 not in book.silences() and book.get(noisy.item_id).suppressed_by is None


def test_inhibition_shows_the_cause_not_the_symptoms(book: TriageBook) -> None:
    cause = book.ingest(sig("command.adapter_error", "cmd-1", robot="arm-1")).item
    same = book.ingest(sig("command.rejected", "cmd-2", robot="arm-1", reason="busy",
                           evidence=("command:cmd-2",))).item
    other = book.ingest(sig("command.rejected", "cmd-3", robot="arm-2", reason="busy",
                            evidence=("command:cmd-3",))).item
    assert book.get(same.item_id).suppressed_by.startswith("inhibition:")
    assert book.get(other.item_id).suppressed_by is None
    book.acknowledge(cause.item_id, "op:a")
    book.resolve(cause.item_id, "op:a", "replaced cable")
    book.refresh_suppression()
    assert book.get(same.item_id).suppressed_by is None


def test_state_is_rebuilt_from_the_ledger(project: Project, book: TriageBook,
                                          clock: Clock) -> None:
    i = book.ingest(sig()).item
    book.acknowledge(i.item_id, "op:a")
    fresh = TriageBook(Project.open(project.root), now=clock)
    assert fresh.get(i.item_id[:8]).state is TriageState.TRIAGED
    assert project.ledger.verify().ok
    kinds = [e.kind for e in project.ledger.query(prefix="triage.")]
    assert kinds == ["triage.opened", "triage.transition"]


def test_policy_round_trip_and_matchers() -> None:
    p = default_policy()
    assert TriagePolicy.from_json(p.to_json()) == p
    assert Matcher.parse("reason!=busy").matches({"reason": "stale"})
    assert Matcher.parse("kind=~command\\.(a|b)").matches({"kind": "command.a"})
    assert not Matcher.parse("kind=~command").matches({"kind": "command.a"})  # anchored
    assert Matcher.parse("x!~y.*").matches({"x": "z"})
    with pytest.raises(SchemaError):
        Matcher.parse("no-operator")


def test_runtime_sink_maps_outcomes(book: TriageBook) -> None:
    sink = RuntimeTriageSink(book)

    def ev(kind: str, subject: str, **data: object) -> TraceEvent:
        return TraceEvent(0, 0.0, "test", kind, subject, dict(data), trace_id="t" * 32)

    sink(ev("submitted", "cmd-1", robot_id="carrier-1", skill_id="dock_at"))
    sink(ev("unresolved", "cmd-1", reason="deadline_exceeded"))
    sink(ev("cancelled", "cmd-2", reason="cancelled", robot_id="carrier-1",
            stopped_confirmed=True))                        # confirmed stop: no item
    sink(ev("cancelled", "cmd-3", reason="cancelled_before_start"))
    sink(ev("cancelled", "cmd-4", reason="cancelled", robot_id="carrier-1",
            stopped_confirmed=False))
    sink(ev("verified", "cmd-5", robot_id="carrier-1"))
    kinds = {(i.kind, i.severity) for i in book.items()}
    assert kinds == {("command.unresolved", Severity.MISSION),
                     ("command.cancelled", Severity.SAFETY)}
    unresolved = next(i for i in book.items() if i.kind == "command.unresolved")
    assert unresolved.robot_id == "carrier-1" and unresolved.labels["skill_id"] == "dock_at"
    assert f"trace:{'t' * 32}" in unresolved.evidence


def test_expired_occupied_lease_becomes_an_item(book: TriageBook) -> None:
    clock = ManualClock()
    res = ResourceManager(clock)
    res.on_change(resource_listener(book))
    res.set_occupancy("handoff", Occupancy.OCCUPIED)
    res.reserve("handoff", "carrier-1", lease_s=5)
    clock.advance(10)
    res.holder("handoff")                       # expiry is evaluated lazily
    res.reserve("handoff", "carrier-1", lease_s=5)
    [item] = book.items()
    assert item.kind == "resource.expired_occupied" and item.priority is Priority.P1


def _propose(project: Project, pid: str = "prop_1") -> Proposal:
    p = Proposal(proposal_id=pid, task_id="t1", kind=ProposalKind.REPORT, title="report",
                 summary="s", produced_by="agent:work", requirements=("REQ06",))
    project.ledger.append("work.proposed", pid, {"proposal": p.to_dict(),
                                                 "activity_id": "act_x"})
    return p


def test_sweep_collects_work_signals_and_auto_resolves(project: Project, book: TriageBook,
                                                       clock: Clock) -> None:
    _propose(project)
    project.ledger.append("work.approval", "act_x", {"call_id": "c1", "tool": "run_python",
                                                     "side_effect": "external",
                                                     "approved": False,
                                                     "approver": "policy:default-deny",
                                                     "reason": "not pre-approved"})
    project.ledger.append("calibration.invalidated", "camera-1", {"robot_id": "arm-1"})
    first = sweep(book, checks=("ledger", "work", "proposals", "requirements", "sources"))
    assert "requirements" in first.skipped and "sources" in first.skipped
    kinds = {i.kind for i in book.items()}
    assert kinds == {"work.proposal_pending", "work.approval_denied",
                     "calibration.invalidated"}
    again = sweep(book)
    assert again.actions.get("opened", 0) == 0 and again.actions.get("occurred", 0) == 0
    decide(project, "prop_1", by="engineer:sam", approve=True)
    third = sweep(book)
    assert [i.kind for i in third.auto_resolved] == ["work.proposal_pending"]
    pending = [i for i in book.items(include_closed=True) if i.kind == "work.proposal_pending"]
    assert pending[0].state is TriageState.RESOLVED


def test_sweep_reports_ledger_tampering_as_integrity(project: Project,
                                                     book: TriageBook) -> None:
    project.ledger.append("activity.note", "x", {"note": "hello"})
    path = project.air_dir / "ledger.jsonl"
    lines = path.read_text("utf-8").splitlines()
    lines[-1] = lines[-1].replace("hello", "HELLO")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    report = sweep(TriageBook(Project.open(project.root), now=book.now), checks=("ledger",))
    assert report.actions["opened"] == 1


def test_cli_end_to_end(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    root = ["--project", str(project.root)]
    _propose(project)
    assert main(["triage", "sweep", *root, "--check", "proposals"]) == 0
    assert "opened 1" in capsys.readouterr().out
    assert main(["triage", "list", *root]) == 0
    out = capsys.readouterr().out
    item_id = out.split()[0]
    assert "proposal awaiting a decision" in out
    assert main(["triage", "ack", item_id, "--by", "reviewer:ana", *root]) == 0
    assert main(["triage", "show", item_id, *root]) == 0
    assert "new -> triaged by reviewer:ana" in capsys.readouterr().out
    assert main(["triage", "silence", "add", "--match", "kind=work.proposal_pending",
                 "--for", "2h", "--by", "reviewer:ana", "--comment", "batch review",
                 *root]) == 0
    assert main(["triage", "silence", "list", *root]) == 0
    assert main(["triage", "rules", *root]) == 0
    assert "stop-not-confirmed" in capsys.readouterr().out
    assert main(["triage", "rules", "--write", *root]) == 0
    assert (project.air_dir / "triage.json").exists()
    assert main(["triage", "resolve", item_id, "--by", "reviewer:ana", "--reason",
                 "decided offline", *root]) == 0
    assert main(["triage", "list", "--all", "--json", *root]) == 0


def test_context_pack_lists_open_items(project: Project, book: TriageBook) -> None:
    from agentic_robotics.provenance.context_pack import build_context_pack

    book.ingest(sig("command.interrupt_failed", "cmd-2"))
    text = build_context_pack(project.root)
    assert "## Open triage" in text and "safety/P1" in text


def test_two_sided_inhibition_does_not_hide_itself(project: Project, clock: Clock) -> None:
    from agentic_robotics.triage import Inhibition

    policy = TriagePolicy(inhibitions=(Inhibition(
        "same-kind", source=(Matcher.parse(r"kind=~probe\..*"),),
        target=(Matcher.parse(r"kind=~probe\..*"),), equal=("robot_id",)),))
    book = TriageBook(project, policy, now=clock)
    a = book.ingest(sig("probe.a", "x", evidence=("e:1",))).item
    b = book.ingest(sig("probe.b", "y", evidence=("e:2",))).item
    assert book.get(a.item_id).suppressed_by is None
    assert book.get(b.item_id).suppressed_by is None


def test_fingerprint_is_full_and_records_missing_group_labels(book: TriageBook) -> None:
    item = book.ingest(sig("command.adapter_error", "cmd-1", robot=None)).item  # type: ignore[arg-type]
    assert len(item.fingerprint) == 64 and item.grouping_version == 1
    assert item.missing_group_labels == ("robot_id",)


def test_changed_repeats_update_and_exact_repeats_are_dropped(book: TriageBook) -> None:
    a = book.ingest(Signal("sources.problem", "x/y", "checks.sources", "old text",
                           evidence=("check:1",))).item
    same = book.ingest(Signal("sources.problem", "x/y", "checks.sources", "old text",
                              evidence=("check:1",)))
    changed = book.ingest(Signal("sources.problem", "x/y", "checks.sources", "new text",
                                 evidence=("check:1",)))
    assert same.action == "unchanged"
    assert changed.action == "occurred" and changed.item.title == "new text"
    assert changed.item.item_id == a.item_id


def test_severity_only_rises_and_human_priority_is_locked(project: Project,
                                                          clock: Clock) -> None:
    from agentic_robotics.triage import TriageRule

    low = TriageRule("low", Severity.QUALITY, Priority.P3, kinds=("probe",),
                     matchers=(Matcher.parse("level=low"),), group_by=("kind", "subject"),
                     ack_within_s=60)
    high = TriageRule("high", Severity.MISSION, Priority.P1, kinds=("probe",),
                      group_by=("kind", "subject"), ack_within_s=60)
    book = TriageBook(project, TriagePolicy(rules=(low, high)), now=clock)
    i = book.ingest(sig("probe", "s", evidence=("e:1",), level="low")).item
    assert (i.severity, i.priority) == (Severity.QUALITY, Priority.P3)
    up = book.ingest(sig("probe", "s", evidence=("e:2",), level="high")).item
    assert (up.severity, up.priority) == (Severity.MISSION, Priority.P1)
    again = book.ingest(sig("probe", "s", evidence=("e:3",), level="low")).item
    assert again.severity is Severity.MISSION              # never lowered automatically
    book.set_priority(i.item_id, Priority.P4, "lead:ana", "known, scheduled for Monday")
    clock.t += 120
    [esc] = book.escalate_overdue()
    assert esc.priority is Priority.P4 and esc.priority_locked
    with pytest.raises(TriageError):
        book.set_priority(i.item_id, Priority.P2, "lead:ana", " ")


@escalation
def test_escalation_raises_priority(book: TriageBook, clock: Clock) -> None:
    i = book.ingest(sig("command.failed", "cmd-1", reason="x")).item   # P2, ack 4 h
    clock.t += 5 * 3600
    [e] = book.escalate_overdue()
    assert (i.priority, e.priority) == (Priority.P2, Priority.P1)


def test_recurring_dismissed_protected_item_is_reopened(book: TriageBook) -> None:
    i = book.ingest(sig("command.interrupt_failed", "cmd-1")).item
    book.dismiss(i.item_id, "officer:kim", "test bench, no hardware attached")
    back = book.ingest(sig("command.interrupt_failed", "cmd-1", evidence=("command:x",)))
    assert back.action == "reopened" and back.item.state is TriageState.NEW
    noisy = book.ingest(sig("command.rejected", "c", reason="busy", evidence=("e:1",))).item
    book.dismiss(noisy.item_id, "op:a", "expected while docking")
    assert book.ingest(sig("command.rejected", "c", reason="busy",
                           evidence=("e:2",))).item.state is TriageState.DISMISSED


def test_protected_items_must_be_acknowledged_before_resolving(book: TriageBook) -> None:
    i = book.ingest(sig("command.interrupt_failed", "cmd-1")).item
    with pytest.raises(TriageError, match="acknowledge"):
        book.resolve(i.item_id, "officer:kim", "stopped by hand", ["ledger:3"])
    book.acknowledge(i.item_id, "officer:kim")
    assert book.resolve(i.item_id, "officer:kim", "stopped by hand",
                        ["ledger:3"]).state is TriageState.RESOLVED


def test_silence_limits(book: TriageBook) -> None:
    with pytest.raises(TriageError, match="empty"):
        book.silence([Matcher.parse("robot_id=~.*")], duration_s=60, by="op:a", comment="x")
    with pytest.raises(TriageError, match="empty"):
        book.silence([Matcher.parse("reason!=busy")], duration_s=60, by="op:a", comment="x")
    with pytest.raises(TriageError, match="duration"):
        book.silence([Matcher.parse("robot_id=arm-1")], duration_s=30 * 86400, by="op:a",
                     comment="forever")


def test_snooze_wakes_on_time_or_count_and_never_for_protected(book: TriageBook,
                                                               clock: Clock) -> None:
    safety = book.ingest(sig("command.interrupt_failed", "cmd-1")).item
    with pytest.raises(TriageError):
        book.snooze(safety.item_id, "op:a", "later", for_s=60)
    q = book.ingest(sig("work.model_refused", "a", category="c", evidence=("e:1",))).item
    with pytest.raises(TriageError):
        book.snooze(q.item_id, "op:a", "later")                 # no wake condition
    s = book.snooze(q.item_id, "op:a", "waiting for model update", for_s=3600)
    assert s.state is TriageState.SNOOZED and not s.overdue(clock.t + 10**7)
    clock.t += 3601
    [woken] = book.wake_snoozed()
    assert woken.state is TriageState.NEW
    book.snooze(q.item_id, "op:a", "only if it keeps happening", wake_after_count=2)
    assert book.ingest(sig("work.model_refused", "b", category="c",
                           evidence=("e:2",))).action == "occurred"
    assert book.ingest(sig("work.model_refused", "c", category="c",
                           evidence=("e:3",))).action == "woken"


def test_checks_that_cannot_run_mark_items_stale_not_resolved(project: Project,
                                                              book: TriageBook) -> None:
    catalog = project.root / "docs" / "research" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"$schema": "ar.source_catalog/1.0", "sources": [{"url": '
                       '"https://github.com/a/b", "host": "github", "owner": "a", "name": "b",'
                       ' "area": "x", "priority": "P0"}]}', encoding="utf-8")
    sweep(book, checks=("sources",))
    [item] = book.items()
    catalog.unlink()
    report = sweep(book, checks=("sources",))
    assert "sources" in report.skipped
    stale = book.get(item.item_id)
    assert stale.state.open and stale.stale


def test_legacy_fingerprints_are_rekeyed_not_duplicated(project: Project,
                                                        book: TriageBook) -> None:
    from agentic_robotics.triage.book import _legacy_fingerprint
    from agentic_robotics.triage.contracts import TriageItem

    s = sig("command.unresolved", "cmd-1")
    old = TriageItem(item_id="tri_legacy", fingerprint=_legacy_fingerprint(s, ("kind", "subject")),
                     kind=s.kind, source=s.source, subject=s.subject, title=s.title,
                     severity=Severity.MISSION, priority=Priority.P1)
    project.ledger.append("triage.opened", old.item_id, {"item": old.to_dict()})
    res = book.ingest(sig("command.unresolved", "cmd-1", evidence=("command:again",)))
    assert res.item.item_id == "tri_legacy" and res.item.grouping_version == 1
    assert len(book.items()) == 1
