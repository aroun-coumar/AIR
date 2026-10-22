"""Debugging aids: timelines and plain-language explanations of runtime outcomes.

Works on runtime trace files (``*.trace.jsonl``) and on runtime events copied into a
project ledger (``runtime.<kind>`` entries), so a failure can be investigated after the
fact from the evidence alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .ledger import LedgerEntry

__requirements__ = ("REQ14", "REQ15")

# Stable rejection/outcome reasons -> what to check next.
REMEDIES: dict[str, str] = {
    "unknown_robot": "Register the robot with Runtime.register() before submitting.",
    "unknown_skill": "Register the skill in the SkillRegistry, or fix the skill id.",
    "unsupported_skill": "The adapter does not implement this skill; choose another robot "
                         "or extend the adapter's supported_skills().",
    "invalid_arguments": "Arguments do not match the SkillSpec inputs; see 'problems'.",
    "authority_revoked": "The delegation (or a parent) was revoked; obtain a new grant.",
    "authority_expired": "The delegation is outside its validity window.",
    "authority_out_of_scope": "The delegation does not cover this robot/skill/operation.",
    "authority_wrong_principal": "issued_by does not match the delegation's grantee.",
    "authority_stale_revision": "Authority changed after the command was issued; re-plan "
                                "and reissue under the current authority revision.",
    "clock_quality": "Clock synchronization is below what the skill's execution contract "
                     "allows; check time sync before retrying.",
    "stale_capability": "The capability report is older than max_input_age_s; check the "
                        "adapter's health reporting.",
    "capability_revision_changed": "The robot's capability changed since planning; re-plan.",
    "capability_unavailable": "A required capability is unavailable; see limiting_factors.",
    "stale_input": "An input observation is older than max_input_age_s; check the sensor "
                   "pipeline and clock domain.",
    "invalid_input": "An input observation has quality=invalid.",
    "precondition_failed": "A precondition is not satisfied by current observations.",
    "missing_fencing_token": "Reserve the exclusive resource and pass its fencing token.",
    "no_active_reservation": "The reservation was released or expired; reserve again.",
    "stale_fencing_token": "A newer owner holds the resource; this command was fenced out.",
    "not_reservation_holder": "The resource is reserved by a different participant.",
    "robot_busy": "The robot is executing another command (queue policy reject_if_busy).",
    "command_id_reused": "Command ids must be unique per request.",
    "postcondition_failed": "Execution finished but observations contradict the expected "
                            "outcome; inspect 'verification'.",
    "deadline_exceeded": "Outcome UNKNOWN. Do not retry blindly: observe the world and call "
                         "Runtime.reconcile(command_id). Reservations stay held.",
    "outcome_unknown": "The adapter lost contact after start. Outcome UNKNOWN: reconcile "
                       "by observation before repeating or reassigning the action.",
    "verification_inconclusive": "Observations were missing or predate execution; refresh "
                                 "observations and reconcile.",
}


@dataclass
class Event:
    t: float
    kind: str
    subject: str
    data: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None


def from_trace_dicts(rows: Iterable[dict[str, Any]]) -> list[Event]:
    return [Event(float(r.get("t", 0.0)), str(r["kind"]), str(r["subject"]),
                  dict(r.get("data") or {}), r.get("trace_id")) for r in rows]


def from_ledger(entries: Iterable[LedgerEntry]) -> list[Event]:
    out = []
    for e in entries:
        if e.kind.startswith("runtime."):
            out.append(Event(float(e.data.get("t", e.ts)), e.kind.removeprefix("runtime."),
                             e.subject, dict(e.data), e.trace_id))
        else:
            out.append(Event(e.ts, e.kind, e.subject, dict(e.data), e.trace_id))
    return out


def _summary(ev: Event) -> str:
    d = ev.data
    keys = ("reason", "skill_id", "robot_id", "issued_by", "message", "remaining_m",
            "stopped_confirmed", "completed", "state", "holder", "fencing_token", "outcome",
            "plan_id", "title", "status")
    parts = [f"{k}={d[k]}" for k in keys if k in d and d[k] not in (None, "")]
    return " ".join(parts)


def timeline(events: list[Event], *, subject: str | None = None,
             trace_id: str | None = None) -> list[str]:
    selected = [e for e in events
                if (subject is None or e.subject == subject)
                and (trace_id is None or e.trace_id == trace_id)]
    if not selected:
        return []
    t0 = selected[0].t
    return [f"+{e.t - t0:9.4f}s  {e.kind:<34} {e.subject:<28} {_summary(e)}".rstrip()
            for e in selected]


def explain(events: list[Event], subject: str) -> list[str]:
    mine = [e for e in events if e.subject == subject]
    if not mine:
        return [f"no events found for {subject!r}"]
    out = []
    first = mine[0].data
    header = f"{subject}"
    if "skill_id" in first:
        header += f": {first.get('skill_id')} on {first.get('robot_id')} " \
                  f"issued by {first.get('issued_by')}"
    out.append(header)
    trace_id = next((e.trace_id for e in mine if e.trace_id), None)
    if trace_id:
        out.append(f"trace: {trace_id}")
    out.append("")
    out.append("timeline:")
    out.extend("  " + line for line in timeline(events, subject=subject))

    terminal = {"rejected", "verified", "failed", "cancelled", "unresolved",
                "reported_complete", "killed", "skipped", "approximate"}
    final = next((e for e in reversed(mine) if e.kind in terminal), None)
    out.append("")
    if final is None:
        out.append("outcome: still in progress (no terminal event recorded)")
    else:
        reason = final.data.get("reason")
        out.append(f"outcome: {final.kind}" + (f" ({reason})" if reason else ""))
        rejected = next((e for e in mine if e.kind == "rejected"), None)
        if rejected is not None:
            details = rejected.data.get("details") or {}
            out.append(f"why: {rejected.data.get('message', '')}")
            for k, v in details.items():
                out.append(f"  {k}: {json.dumps(v, default=str)}")
        if reason and reason in REMEDIES:
            out.append(f"next step: {REMEDIES[reason]}")
        for v in (e for e in mine if e.kind == "verification"):
            for item in v.data.get("results", []):
                desc, ok = (item[0], item[1]) if isinstance(item, (list, tuple)) else (item, None)
                out.append(f"  check {'ok  ' if ok else 'FAIL'} {desc}")
    snap = next((e for e in mine if e.kind == "admission_snapshot"), None)
    if snap is not None:
        out.append("")
        out.append("admission snapshot (debug mode):")
        for k, v in snap.data.items():
            out.append(f"  {k}: {json.dumps(v, default=str)}")
    if trace_id:
        related = sorted({e.subject for e in events
                          if e.trace_id == trace_id and e.subject != subject})
        if related:
            out.append("")
            out.append("same trace: " + ", ".join(related))
    return out
