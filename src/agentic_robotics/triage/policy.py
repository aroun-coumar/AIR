"""Triage policy as data: routing rules, inhibitions and silences (ADR 0009).

Patterns adopted (re-implemented, not depended on):

- **Label matchers** ``=``, ``!=``, ``=~``, ``!~`` and first-match routing with
  ``group_by`` labels: Prometheus Alertmanager routes.
- **Inhibition** (an open source item suppresses target items sharing ``equal`` labels)
  and **silences** (matchers, start/end, creator, comment): Alertmanager.
- **Fingerprints** from a chosen set of fields, so duplicates fold into one item:
  Alertmanager label-set fingerprints, Sentry custom fingerprints, Keep
  ``fingerprint_fields``.
- **Escalation chains** with an acknowledgement deadline: Grafana OnCall.

AIR's addition: protected severities (safety, integrity) can never be silenced or
inhibited, so a noisy filter cannot hide something that matters physically.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field

from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from .contracts import Priority, Severity, Signal

__requirements__ = ("REQ14", "REQ15", "REQ17")


@dataclass(frozen=True)
class Matcher:
    """``name op value`` over a signal's labels (plus kind, source, subject, robot_id)."""

    name: str
    value: str
    op: str = "="

    def __post_init__(self) -> None:
        if self.op not in ("=", "!=", "=~", "!~"):
            raise SchemaError("unknown matcher operator", op=self.op)
        if self.op in ("=~", "!~"):
            re.compile(self.value)

    def matches(self, labels: dict[str, str]) -> bool:
        actual = labels.get(self.name, "")
        if self.op == "=":
            return actual == self.value
        if self.op == "!=":
            return actual != self.value
        hit = re.fullmatch(self.value, actual) is not None   # anchored, as in Alertmanager
        return hit if self.op == "=~" else not hit

    @classmethod
    def parse(cls, text: str) -> Matcher:
        """``robot_id=arm-1``, ``kind=~command\\..*``, ``reason!=busy``."""
        for op in ("=~", "!~", "!=", "="):
            name, sep, value = text.partition(op)
            if sep and name and "=" not in name and "!" not in name and "~" not in name:
                return cls(name.strip(), value.strip(), op)
        raise SchemaError("matcher must look like name=value, name!=value, name=~regex "
                          "or name!~regex", matcher=text)

    def __str__(self) -> str:
        return f"{self.name}{self.op}{self.value}"


def all_match(matchers: tuple[Matcher, ...], labels: dict[str, str]) -> bool:
    return all(m.matches(labels) for m in matchers)


@dataclass(frozen=True)
class TriageRule:
    """First matching rule wins (kinds are globs; all matchers must hold)."""

    name: str
    severity: Severity
    priority: Priority
    kinds: tuple[str, ...] = ("*",)
    matchers: tuple[Matcher, ...] = ()
    owner: str = "role:operator"
    escalation: tuple[str, ...] = ()     # owners after `owner`, in order
    ack_within_s: float | None = None
    resolve_within_s: float | None = None
    group_by: tuple[str, ...] = ("kind", "subject")
    auto_resolve: bool = False           # sweep-checked conditions resolve when they clear

    def matches(self, labels: dict[str, str]) -> bool:
        return any(fnmatch.fnmatchcase(labels.get("kind", ""), k) for k in self.kinds) \
            and all_match(self.matchers, labels)


@dataclass(frozen=True)
class Inhibition:
    """While an open item matches ``source``, items matching ``target`` with the same
    values for ``equal`` labels are suppressed (never protected-severity targets)."""

    name: str
    source: tuple[Matcher, ...]
    target: tuple[Matcher, ...]
    equal: tuple[str, ...] = ()


@dataclass(frozen=True)
class Silence:
    silence_id: str
    matchers: tuple[Matcher, ...]
    starts_at: float
    ends_at: float
    created_by: str
    comment: str

    def active(self, now: float) -> bool:
        return self.starts_at <= now < self.ends_at


@contract("triage_policy", version="1.0")
class TriagePolicy(Contract):
    rules: tuple[TriageRule, ...] = ()
    inhibitions: tuple[Inhibition, ...] = ()
    default: TriageRule = field(default_factory=lambda: TriageRule(
        "default", Severity.QUALITY, Priority.P3, owner="role:maintainer",
        ack_within_s=7 * 86400.0))
    max_silence_s: float = 7 * 86400.0
    max_snooze_s: float = 30 * 86400.0

    def __post_init__(self) -> None:
        names = [r.name for r in self.rules]
        if len(names) != len(set(names)):
            raise SchemaError("duplicate triage rule names")

    def route(self, signal: Signal) -> TriageRule:
        labels = signal_labels(signal)
        return next((r for r in self.rules if r.matches(labels)), self.default)


def signal_labels(signal: Signal) -> dict[str, str]:
    labels = dict(signal.labels)
    labels.update(kind=signal.kind, source=signal.source, subject=signal.subject)
    if signal.robot_id:
        labels["robot_id"] = signal.robot_id
    return labels


GROUPING_VERSION = 1


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def fingerprint_info(signal: Signal, group_by: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    """Full SHA-256 fingerprint over ``group_by`` labels, plus the labels that were absent
    (recorded on the item, so silent grouping by an empty value is visible)."""
    labels = signal_labels(signal)
    names = sorted(set(group_by) | {"kind"})
    missing = tuple(n for n in names if n not in labels)
    key = {"v": GROUPING_VERSION, **{n: labels.get(n, "") for n in names}}
    return _digest(key), missing


def fingerprint(signal: Signal, group_by: tuple[str, ...]) -> str:
    return fingerprint_info(signal, group_by)[0]


def content_hash(signal: Signal) -> str:
    """What the signal says (title and labels), as in Keep's full vs partial duplicates."""
    return _digest({"title": signal.title, "labels": signal.labels})


def m(text: str) -> Matcher:
    return Matcher.parse(text)


_MIN, _HOUR, _DAY = 60.0, 3600.0, 86400.0
_ESC = ("role:shift-lead", "role:safety-officer")


def default_policy() -> TriagePolicy:
    """The built-in routing table. Projects override it with ``.air/triage.json``."""
    R, S, P = TriageRule, Severity, Priority
    return TriagePolicy(rules=(
        # Physical outcome unknown after a cancel or interrupt: someone must look now.
        R("stop-not-confirmed", S.SAFETY, P.P1, kinds=("command.cancelled",),
          matchers=(m("stopped_confirmed!=true"),), owner="role:operator", escalation=_ESC,
          ack_within_s=5 * _MIN, resolve_within_s=1 * _HOUR,
          group_by=("kind", "subject")),
        R("interrupt-failed", S.SAFETY, P.P1, kinds=("command.interrupt_failed",),
          owner="role:operator", escalation=_ESC, ack_within_s=5 * _MIN,
          resolve_within_s=1 * _HOUR),
        R("unresolved-outcome", S.MISSION, P.P1, kinds=("command.unresolved",),
          owner="role:operator", escalation=_ESC, ack_within_s=15 * _MIN,
          resolve_within_s=4 * _HOUR),
        R("revoked-mid-execution", S.MISSION, P.P2,
          kinds=("command.authority_revoked_during_execution",), owner="role:operator",
          escalation=_ESC[:1], ack_within_s=30 * _MIN),
        R("adapter-error", S.MISSION, P.P2, kinds=("command.adapter_error",),
          owner="role:integrator", escalation=_ESC[:1], ack_within_s=1 * _HOUR,
          group_by=("kind", "robot_id")),
        R("command-failed", S.MISSION, P.P2, kinds=("command.failed",),
          owner="role:operator", ack_within_s=4 * _HOUR,
          group_by=("kind", "robot_id", "skill_id", "reason")),
        # A lease expired while the resource may still be occupied: it stays blocked
        # (fail closed) until someone confirms the physical state.
        R("expired-occupied", S.MISSION, P.P1, kinds=("resource.expired_occupied",),
          owner="role:operator", escalation=_ESC[:1], ack_within_s=15 * _MIN,
          resolve_within_s=2 * _HOUR),
        # Rejections are usually correct behaviour; group them so patterns show up.
        R("admission-rejected", S.HYGIENE, P.P4, kinds=("command.rejected",),
          owner="role:integrator", group_by=("kind", "robot_id", "reason")),
        R("ledger-integrity", S.INTEGRITY, P.P1, kinds=("ledger.integrity",),
          owner="role:maintainer", escalation=_ESC[1:], ack_within_s=1 * _HOUR,
          auto_resolve=False),
        R("calibration-invalidated", S.MISSION, P.P2, kinds=("calibration.invalidated",),
          owner="role:maintenance", ack_within_s=1 * _DAY),
        R("proposal-pending", S.QUALITY, P.P3, kinds=("work.proposal_pending",),
          owner="role:reviewer", ack_within_s=2 * _DAY, auto_resolve=True),
        R("model-refused", S.QUALITY, P.P3, kinds=("work.model_refused",),
          owner="role:maintainer", group_by=("kind", "category")),
        R("sandbox-refused", S.QUALITY, P.P3, kinds=("work.sandbox_refused",),
          owner="role:maintainer", group_by=("kind", "subject")),
        R("approval-denied", S.HYGIENE, P.P4, kinds=("work.approval_denied",),
          owner="role:reviewer", group_by=("kind", "tool")),
        R("requirement-evidence", S.QUALITY, P.P3, kinds=("requirements.check",),
          owner="role:maintainer", ack_within_s=7 * _DAY, auto_resolve=True),
        R("source-catalog", S.HYGIENE, P.P4, kinds=("sources.problem",),
          owner="role:maintainer", auto_resolve=True),
    ), inhibitions=(
        # A robot whose adapter is failing will also reject and fail commands; show the
        # cause, not the symptoms (Alertmanager inhibition).
        Inhibition("adapter-error-hides-symptoms", source=(m("kind=command.adapter_error"),),
                   target=(m("kind=~command\\.(rejected|failed)"),), equal=("robot_id",)),
    ))
