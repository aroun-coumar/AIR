"""The triage book: an event-sourced queue over the project ledger (ADR 0009).

Every change is a ledger entry, so triage history is as tamper-evident as the rest of
the project's evidence:

- ``triage.opened``      a new item (the first signal for a fingerprint)
- ``triage.occurred``    the same fingerprint seen again (count, last seen, evidence)
- ``triage.transition``  a state change with who, why and evidence
- ``triage.suppressed``  a silence or inhibition applied or lifted
- ``triage.silenced`` / ``triage.unsilenced``  silences

State is rebuilt by replaying these entries, so any process can open the book.

Lifecycle rules (adopted, then tightened):

- A resolved fingerprint that comes back is **regressed** (Sentry regressions).
- Unacknowledged items **escalate** along the rule's chain when the acknowledgement
  deadline passes, and once more if the resolve deadline passes (Grafana OnCall).
- Sweep-checked conditions with ``auto_resolve`` resolve themselves when they clear
  (Prometheus/Alertmanager resolved alerts), except protected severities, which need
  a person.
- Dismissing needs a reason. Resolving a safety item needs an evidence reference.
  Agents (``agent:*``) cannot resolve or dismiss safety or integrity items; they can
  only acknowledge, comment through evidence, or work them (AIR principle: agents
  propose, people and the runtime dispose).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import AuthorityError, FrameworkError
from ..core.ids import new_id
from ..provenance.project import Project
from .contracts import TRANSITIONS, Priority, Severity, Signal, TriageItem, TriageState
from .policy import (
    GROUPING_VERSION,
    Matcher,
    Silence,
    TriagePolicy,
    all_match,
    content_hash,
    default_policy,
    fingerprint_info,
    signal_labels,
)

__requirements__ = ("REQ14", "REQ15", "REQ17", "G11", "G12")

SYSTEM = "system:triage"
POLICY_FILE = "triage.json"


class TriageError(FrameworkError):
    code = "triage_error"


@dataclass
class IngestResult:
    item: TriageItem
    action: str          # opened | occurred | regressed | unchanged


def is_agent(principal: str) -> bool:
    return principal.startswith("agent:")


def load_policy(project: Project) -> TriagePolicy:
    path = project.air_dir / POLICY_FILE
    return TriagePolicy.load(path) if path.exists() else default_policy()


class TriageBook:
    def __init__(self, project: Project, policy: TriagePolicy | None = None,
                 now: Callable[[], float] = time.time) -> None:
        self.project = project
        self.policy = policy or load_policy(project)
        self.now = now
        self._items: dict[str, TriageItem] = {}
        self._by_fp: dict[str, str] = {}
        self._silences: dict[str, Silence] = {}
        self._replayed = -1

    # -- replay ------------------------------------------------------------------------
    def _replay(self) -> None:
        ledger = self.project.ledger
        if len(ledger) - 1 == self._replayed:
            return
        for e in ledger.query(prefix="triage."):
            if e.seq <= self._replayed:
                continue
            d = e.data
            if e.kind == "triage.opened":
                item = TriageItem.from_dict(d["item"])
                self._items[item.item_id] = item
                self._by_fp[item.fingerprint] = item.item_id
            elif e.kind in ("triage.occurred", "triage.transition", "triage.suppressed"):
                self._items[e.subject] = self._items[e.subject].evolve(**_load(d["set"]))
            elif e.kind == "triage.silenced":
                s = d["silence"]
                self._silences[e.subject] = Silence(
                    s["silence_id"], tuple(Matcher(**x) for x in s["matchers"]),
                    s["starts_at"], s["ends_at"], s["created_by"], s["comment"])
            elif e.kind == "triage.unsilenced" and e.subject in self._silences:
                old = self._silences[e.subject]
                self._silences[e.subject] = Silence(
                    old.silence_id, old.matchers, old.starts_at, d["ended_at"],
                    old.created_by, old.comment)
        self._replayed = len(ledger) - 1

    def _write(self, kind: str, item_id: str, data: dict[str, Any]) -> None:
        self.project.ledger.append(kind, item_id, data)
        self._replay()

    def _update(self, kind: str, item: TriageItem, extra: dict[str, Any] | None = None,
                **changes: Any) -> TriageItem:
        self._write(kind, item.item_id, {"set": _dump(changes), **(extra or {})})
        return self._items[item.item_id]

    # -- queries -----------------------------------------------------------------------
    def items(self, *, include_closed: bool = False, include_suppressed: bool = False,
              severity: Severity | None = None, owner: str | None = None,
              overdue: bool = False) -> list[TriageItem]:
        self._replay()
        now = self.now()
        out = [i for i in self._items.values()
               if (include_closed or i.state.open)
               and (include_suppressed or i.suppressed_by is None)
               and (severity is None or i.severity is severity)
               and (owner is None or owner in (i.owner, i.assignee))
               and (not overdue or i.overdue(now))]
        return sorted(out, key=lambda i: (i.severity.rank, i.priority.value, -i.last_seen))

    def get(self, item_id: str) -> TriageItem:
        self._replay()
        matches = [i for k, i in self._items.items() if k == item_id or
                   k.startswith(item_id)]
        if len(matches) != 1:
            raise TriageError("no such triage item" if not matches else "ambiguous id",
                              item_id=item_id)
        return matches[0]

    def history(self, item_id: str) -> list[dict[str, Any]]:
        item = self.get(item_id)
        return [{"seq": e.seq, "ts": e.ts, "kind": e.kind, **e.data}
                for e in self.project.ledger.query(subject=item.item_id)
                if e.kind.startswith("triage.")]

    def silences(self, *, active_only: bool = True) -> list[Silence]:
        self._replay()
        now = self.now()
        return [s for s in self._silences.values() if not active_only or s.active(now)]

    # -- ingest ------------------------------------------------------------------------
    def _find(self, signal: Signal, fp: str, group_by: tuple[str, ...]) -> TriageItem | None:
        item = self._items.get(self._by_fp.get(fp, ""))
        if item is not None:
            return item
        legacy = self._items.get(self._by_fp.get(_legacy_fingerprint(signal, group_by), ""))
        if legacy is None:
            return None
        # Re-key an item made by an older grouping version instead of opening a twin.
        self._by_fp[fp] = legacy.item_id
        return self._update("triage.suppressed", legacy, fingerprint=fp,
                            grouping_version=GROUPING_VERSION)

    def ingest(self, signal: Signal) -> IngestResult:
        self._replay()
        rule = self.policy.route(signal)
        fp, missing = fingerprint_info(signal, rule.group_by)
        chash = content_hash(signal)
        now = signal.observed_at or self.now()
        existing = self._find(signal, fp, rule.group_by)
        if existing is None:
            item = TriageItem(
                item_id=new_id("tri"), fingerprint=fp, kind=signal.kind, source=signal.source,
                subject=signal.subject, title=signal.title, severity=rule.severity,
                priority=rule.priority, rule=rule.name, owner=rule.owner,
                escalation=rule.escalation, labels=dict(signal.labels),
                robot_id=signal.robot_id, requirements=signal.requirements,
                evidence=signal.evidence, first_seen=now, last_seen=now,
                ack_due=now + rule.ack_within_s if rule.ack_within_s else None,
                resolve_due=now + rule.resolve_within_s if rule.resolve_within_s else None,
                grouping_version=GROUPING_VERSION, missing_group_labels=missing,
                content_hash=chash)
            self._write("triage.opened", item.item_id, {"item": item.to_dict()})
            self._apply_suppression(self._items[item.item_id])
            return IngestResult(self._items[item.item_id], "opened")

        new_evidence = tuple(e for e in signal.evidence if e not in existing.evidence)
        if signal.evidence and not new_evidence and chash == existing.content_hash:
            return IngestResult(existing, "unchanged")         # exact repeat
        changes: dict[str, Any] = {
            "count": existing.count + 1, "last_seen": now,
            "evidence": (existing.evidence + new_evidence)[-50:],
            "requirements": tuple(dict.fromkeys(existing.requirements + signal.requirements))}
        if chash != existing.content_hash:                     # changed repeat: update it
            changes.update(title=signal.title, labels=dict(signal.labels), content_hash=chash)
        if rule.severity.rank < existing.severity.rank:        # severity only rises
            changes["severity"] = rule.severity
        if not existing.priority_locked:
            changes["priority"] = Priority.most_urgent(existing.priority, rule.priority)
        item = self._update("triage.occurred", existing, **changes)
        if existing.state is TriageState.RESOLVED:
            item = self._transition(item, TriageState.REGRESSED, SYSTEM,
                                    "the same problem was seen again after it was resolved",
                                    deadlines_from=now, rule_ack=rule.ack_within_s,
                                    rule_resolve=rule.resolve_within_s)
            return IngestResult(item, "regressed")
        if existing.state is TriageState.DISMISSED and item.severity.protected:
            item = self._transition(item, TriageState.NEW, SYSTEM,
                                    f"{item.severity.value} item seen again after dismissal",
                                    deadlines_from=now, rule_ack=rule.ack_within_s,
                                    rule_resolve=rule.resolve_within_s, resolution="")
            return IngestResult(item, "reopened")
        if existing.state is TriageState.SNOOZED and existing.wake_after_count is not None \
                and item.count >= existing.wake_after_count:
            return IngestResult(self._wake(item, f"seen {item.count} times"), "woken")
        return IngestResult(item, "occurred")

    # -- transitions -------------------------------------------------------------------
    def _transition(self, item: TriageItem, to: TriageState, by: str, reason: str, *,
                    evidence: Iterable[str] = (), deadlines_from: float | None = None,
                    rule_ack: float | None = None, rule_resolve: float | None = None,
                    **changes: Any) -> TriageItem:
        if to not in TRANSITIONS[item.state]:
            raise TriageError(f"cannot move from {item.state.value} to {to.value}",
                              item_id=item.item_id)
        ev = tuple(evidence)
        if deadlines_from is not None:
            changes["ack_due"] = deadlines_from + rule_ack if rule_ack else None
            changes["resolve_due"] = deadlines_from + rule_resolve if rule_resolve else None
            changes["resolve_escalated"] = False
        if ev:
            changes["evidence"] = item.evidence + tuple(e for e in ev if e not in item.evidence)
        changes["state"] = to
        return self._update("triage.transition", item,
                            {"from": item.state.value, "to": to.value, "by": by,
                             "reason": reason, "evidence_added": list(ev)}, **changes)

    def _check_actor(self, item: TriageItem, by: str, to: TriageState, reason: str,
                     evidence: tuple[str, ...]) -> None:
        if not by or by == SYSTEM:
            raise AuthorityError("a named principal must make triage decisions", by=by)
        if to in (TriageState.RESOLVED, TriageState.DISMISSED):
            if is_agent(by) and item.severity.protected:
                verb = "resolve" if to is TriageState.RESOLVED else "dismiss"
                raise AuthorityError(
                    f"agents cannot {verb} {item.severity.value} items; a person must "
                    "decide", by=by, item_id=item.item_id)
            if not reason.strip():
                raise TriageError(f"{to.value} needs a reason", item_id=item.item_id)
        if to is TriageState.RESOLVED and item.severity.protected \
                and not item.state.acknowledged:
            raise TriageError(f"acknowledge a {item.severity.value} item before resolving it",
                              item_id=item.item_id)
        if to is TriageState.RESOLVED and item.severity is Severity.SAFETY and not evidence:
            raise TriageError("resolving a safety item needs an evidence reference "
                              "(--evidence ledger:<seq>, command:<id>, ...)",
                              item_id=item.item_id)

    def acknowledge(self, item_id: str, by: str, reason: str = "") -> TriageItem:
        item = self.get(item_id)
        self._check_actor(item, by, TriageState.TRIAGED, reason, ())
        return self._transition(item, TriageState.TRIAGED, by, reason or "acknowledged")

    def assign(self, item_id: str, assignee: str, by: str, reason: str = "") -> TriageItem:
        item = self.get(item_id)
        self._check_actor(item, by, TriageState.ASSIGNED, reason, ())
        return self._transition(item, TriageState.ASSIGNED, by,
                                reason or f"assigned to {assignee}", assignee=assignee)

    def start(self, item_id: str, by: str, reason: str = "") -> TriageItem:
        item = self.get(item_id)
        self._check_actor(item, by, TriageState.IN_PROGRESS, reason, ())
        return self._transition(item, TriageState.IN_PROGRESS, by, reason or "work started",
                                assignee=item.assignee or by)

    def resolve(self, item_id: str, by: str, reason: str,
                evidence: Iterable[str] = ()) -> TriageItem:
        item = self.get(item_id)
        ev = tuple(evidence)
        self._check_actor(item, by, TriageState.RESOLVED, reason, ev)
        return self._transition(item, TriageState.RESOLVED, by, reason, evidence=ev,
                                resolution=reason)

    def dismiss(self, item_id: str, by: str, reason: str) -> TriageItem:
        item = self.get(item_id)
        self._check_actor(item, by, TriageState.DISMISSED, reason, ())
        return self._transition(item, TriageState.DISMISSED, by, reason, resolution=reason)

    def reopen(self, item_id: str, by: str, reason: str) -> TriageItem:
        item = self.get(item_id)
        self._check_actor(item, by, TriageState.NEW, reason, ())
        rule = self._rule(item)
        return self._transition(item, TriageState.NEW, by, reason or "reopened",
                                deadlines_from=self.now(), rule_ack=rule.ack_within_s,
                                rule_resolve=rule.resolve_within_s, resolution="")

    def set_priority(self, item_id: str, priority: Priority, by: str,
                     reason: str) -> TriageItem:
        """A person's priority wins: it is locked against rules and escalation."""
        item = self.get(item_id)
        if not by or by == SYSTEM:
            raise AuthorityError("a named principal must set priority", by=by)
        if not reason.strip():
            raise TriageError("changing priority needs a reason", item_id=item.item_id)
        return self._update("triage.transition", item,
                            {"from": item.state.value, "to": item.state.value, "by": by,
                             "reason": f"priority {item.priority.value} -> {priority.value}: "
                                       f"{reason}", "evidence_added": []},
                            priority=priority, priority_locked=True)

    def snooze(self, item_id: str, by: str, reason: str, *, for_s: float | None = None,
               wake_after_count: int | None = None) -> TriageItem:
        item = self.get(item_id)
        self._check_actor(item, by, TriageState.SNOOZED, reason, ())
        if item.severity.protected:
            raise TriageError(f"{item.severity.value} items cannot be snoozed",
                              item_id=item.item_id)
        if for_s is None and wake_after_count is None:
            raise TriageError("snooze needs a wake condition (a duration or a count)")
        if for_s is not None and not 0 < for_s <= self.policy.max_snooze_s:
            raise TriageError("snooze duration out of range", max_s=self.policy.max_snooze_s)
        if not reason.strip():
            raise TriageError("snoozing needs a reason", item_id=item.item_id)
        return self._transition(
            item, TriageState.SNOOZED, by, reason,
            snooze_until=self.now() + for_s if for_s is not None else None,
            wake_after_count=item.count + wake_after_count if wake_after_count else None)

    def _wake(self, item: TriageItem, why: str) -> TriageItem:
        rule = self._rule(item)
        return self._transition(item, TriageState.NEW, SYSTEM, f"snooze ended: {why}",
                                deadlines_from=self.now(), rule_ack=rule.ack_within_s,
                                rule_resolve=rule.resolve_within_s, snooze_until=None,
                                wake_after_count=None)

    def wake_snoozed(self) -> list[TriageItem]:
        self._replay()
        now = self.now()
        return [self._wake(i, "time elapsed") for i in list(self._items.values())
                if i.state is TriageState.SNOOZED and i.snooze_until is not None
                and now >= i.snooze_until]

    def mark_stale(self, source: str, stale: bool) -> None:
        """A check that could not run leaves its items open but marked stale (never
        resolved); the mark clears when the check runs again."""
        self._replay()
        for item in list(self._items.values()):
            if item.source == source and item.state.open and item.stale != stale:
                self._update("triage.suppressed", item, stale=stale)

    def _rule(self, item: TriageItem) -> Any:
        return next((r for r in self.policy.rules if r.name == item.rule), self.policy.default)

    # -- escalation and auto-resolution ------------------------------------------------
    def escalate_overdue(self) -> list[TriageItem]:
        """Move overdue items to the next owner in their chain (Grafana OnCall style)."""
        self._replay()
        now = self.now()
        out = []
        for item in list(self._items.values()):
            if not item.state.open or item.state is TriageState.SNOOZED:
                continue
            rule = self._rule(item)
            ack_missed = not item.state.acknowledged and item.ack_due is not None \
                and now > item.ack_due
            resolve_missed = item.resolve_due is not None and now > item.resolve_due \
                and not item.resolve_escalated
            if not (ack_missed or resolve_missed):
                continue
            level = min(item.escalation_level + 1, len(item.escalation))
            owner = item.escalation[level - 1] if level else item.owner
            why = "acknowledgement deadline passed" if ack_missed \
                else "resolve deadline passed"
            out.append(self._transition(
                item, TriageState.ESCALATED, SYSTEM, f"{why}; now with {owner}",
                owner=owner, escalation_level=level,
                priority=item.priority if item.priority_locked else item.priority.raised(),
                ack_due=now + rule.ack_within_s if rule.ack_within_s else None,
                resolve_escalated=item.resolve_escalated or resolve_missed))
        return out

    def auto_resolve(self, source: str, still_present: set[str]) -> list[TriageItem]:
        """Resolve open items from ``source`` whose condition cleared in this sweep."""
        self._replay()
        out = []
        for item in list(self._items.values()):
            if item.source != source or not item.state.open or item.severity.protected:
                continue
            if not self._rule(item).auto_resolve or item.fingerprint in still_present:
                continue
            out.append(self._transition(item, TriageState.RESOLVED, SYSTEM,
                                        "condition cleared at the last sweep",
                                        resolution="condition cleared"))
        return out

    # -- silences and inhibitions ------------------------------------------------------
    def silence(self, matchers: Iterable[Matcher], *, duration_s: float, by: str,
                comment: str) -> Silence:
        ms = tuple(matchers)
        if not ms:
            raise TriageError("a silence needs at least one matcher")
        if not comment.strip():
            raise TriageError("a silence needs a comment saying why")
        if all(m.matches({}) for m in ms):
            raise TriageError("a silence needs at least one matcher that does not match "
                              "an empty value (it would silence everything)")
        if not 0 < duration_s <= self.policy.max_silence_s:
            raise TriageError("silence duration out of range",
                              max_s=self.policy.max_silence_s)
        if is_agent(by):
            raise AuthorityError("agents cannot create silences", by=by)
        now = self.now()
        s = Silence(new_id("sil"), ms, now, now + duration_s, by, comment)
        self._write("triage.silenced", s.silence_id, {"silence": {
            "silence_id": s.silence_id, "matchers": [m.__dict__ for m in ms],
            "starts_at": s.starts_at, "ends_at": s.ends_at, "created_by": by,
            "comment": comment}})
        self.refresh_suppression()
        return s

    def unsilence(self, silence_id: str, by: str) -> None:
        self._replay()
        if silence_id not in self._silences:
            raise TriageError("no such silence", silence_id=silence_id)
        self._write("triage.unsilenced", silence_id, {"by": by, "ended_at": self.now()})
        self.refresh_suppression()

    def _suppressor(self, item: TriageItem) -> str | None:
        if item.severity.protected:
            return None
        labels = _item_labels(item)
        now = self.now()
        for s in self._silences.values():
            if s.active(now) and all_match(s.matchers, labels):
                return s.silence_id
        for inh in self.policy.inhibitions:
            if not all_match(inh.target, labels):
                continue
            # As in Alertmanager: when the target also matches the source side, sources
            # that match both sides are skipped, so nothing inhibits itself or its twin.
            two_sided = all_match(inh.source, labels)
            for src in self._items.values():
                if src.item_id == item.item_id or not src.state.open \
                        or src.suppressed_by is not None:
                    continue
                src_labels = _item_labels(src)
                if two_sided and all_match(inh.target, src_labels):
                    continue
                if all_match(inh.source, src_labels) and all(
                        src_labels.get(k) == labels.get(k) for k in inh.equal):
                    return f"inhibition:{inh.name}:{src.item_id}"
        return None

    def _apply_suppression(self, item: TriageItem) -> TriageItem:
        want = self._suppressor(item)
        if want != item.suppressed_by:
            item = self._update("triage.suppressed", item, suppressed_by=want)
        return item

    def refresh_suppression(self) -> None:
        self._replay()
        for item in list(self._items.values()):
            if item.state.open:
                self._apply_suppression(item)


def _item_labels(item: TriageItem) -> dict[str, str]:
    return signal_labels(Signal(item.kind, item.subject, item.source, item.title,
                                item.labels, item.robot_id)) | {
        "severity": item.severity.value, "priority": item.priority.value,
        "owner": item.owner}


def _dump(changes: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in changes.items():
        if isinstance(v, tuple):
            v = list(v)
        elif hasattr(v, "value"):
            v = v.value
        out[k] = v
    return out


_FIELD_TYPES = {"state": TriageState, "severity": Severity, "priority": Priority}


def _load(data: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k in _FIELD_TYPES:
            v = _FIELD_TYPES[k](v)
        elif isinstance(v, list):
            v = tuple(v)
        out[k] = v
    return out


def policy_path(project: Project) -> Path:
    return project.air_dir / POLICY_FILE


def _legacy_fingerprint(signal: Signal, group_by: tuple[str, ...]) -> str:
    """Grouping version 0 (early development builds): SHA-256 truncated to 64 bits."""
    labels = signal_labels(signal)
    key = {name: labels.get(name, "") for name in sorted(set(group_by) | {"kind"})}
    return hashlib.sha256(json.dumps(key, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()[:16]
