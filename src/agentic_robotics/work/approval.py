"""Approvals for work-agent tool calls, hardened (catalog W2.5).

* **Bound and single-use.** An :class:`ApprovalRequest` names a pending ``call_id``, the
  tool and the exact digest of its arguments. A decision that names a different call,
  tool or digest is a rejection. A pre-issued :class:`Grant` (an approval given out of
  band, e.g. when resuming after an interrupt) is honoured only for a call that is
  pending now, still requires approval, and matches tool and digest exactly; it is
  consumed on use. Pattern: google/adk-python confirmations, openai-agents-python
  approvals (Apache-2.0 / MIT; no code copied).
* **Approver chains.** Each approver approves, rejects or escalates to the next. If
  nobody decides, the call is rejected and that is recorded (inspect_ai approvers, MIT).
* **Unattended mode.** ``dont_ask`` turns every ask into a deny without consulting
  anyone (agentscope permission modes, Apache-2.0).
* **Bypass-immune asks.** Allow rules can silence ordinary asks, never bypass-immune
  ones (robot actuation, promotion, high or unknown risk, unparseable arguments, a
  command policy ``prompt``).
* **Edits.** An approver may approve *edited* arguments for the same tool; the agent
  re-validates them and tells the model what actually ran (langchain HITL, MIT).
"""

from __future__ import annotations

import fnmatch
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import ApprovalMode, Risk, SideEffect

__requirements__ = ("AIR01", "AIR03", "REQ02", "REQ04", "G15")


class Verdict(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class ApprovalRequest:
    task_id: str
    call_id: str
    tool: str
    side_effect: SideEffect
    arguments: dict[str, Any]
    principal: str
    arguments_digest: str = ""
    reasons: tuple[str, ...] = ()
    risk: Risk = Risk.UNKNOWN
    bypass_immune: bool = False


@dataclass(frozen=True)
class Decision:
    """An approver's answer. ``approved`` alone is the classic form; ``verdict`` may say
    ``escalate``. ``call_id`` / ``tool`` / ``arguments_digest``, when given, bind the
    decision; a mismatch rejects."""

    approved: bool
    approver: str
    reason: str = ""
    verdict: Verdict | None = None
    call_id: str | None = None
    tool: str | None = None
    arguments_digest: str | None = None
    edited_arguments: dict[str, Any] | None = None

    @property
    def outcome(self) -> Verdict:
        if self.verdict is not None:
            return self.verdict
        return Verdict.APPROVE if self.approved else Verdict.REJECT

    @classmethod
    def escalate(cls, approver: str, reason: str = "") -> Decision:
        return cls(False, approver, reason, verdict=Verdict.ESCALATE)


Approver = Callable[[ApprovalRequest], Awaitable[Decision]]


async def deny_all(request: ApprovalRequest) -> Decision:
    return Decision(False, "policy:default-deny", "no approver configured")


@dataclass(frozen=True)
class Grant:
    """An approval issued ahead of the call (e.g. by a person answering an interrupt)."""

    call_id: str
    tool: str
    arguments_digest: str
    approver: str
    reason: str = ""


@dataclass(frozen=True)
class AllowRule:
    """Silences ordinary (not bypass-immune) asks for matching calls."""

    tool: str = "*"
    side_effect: SideEffect | None = None
    name: str = "allow-rule"

    def matches(self, req: ApprovalRequest) -> bool:
        return fnmatch.fnmatchcase(req.tool, self.tool) and (
            self.side_effect is None or self.side_effect is req.side_effect)


@dataclass(frozen=True)
class Step:
    approver: str
    verdict: Verdict
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"approver": self.approver, "verdict": self.verdict.value, "reason": self.reason}


@dataclass(frozen=True)
class Resolution:
    approved: bool
    approver: str
    reason: str
    steps: tuple[Step, ...] = ()
    edited_arguments: dict[str, Any] | None = None


BYPASS_IMMUNE_EFFECTS = frozenset({SideEffect.ROBOT, SideEffect.PROMOTE})


@dataclass
class ApprovalGate:
    approvers: Sequence[Approver] = (deny_all,)
    mode: ApprovalMode = ApprovalMode.ASK
    allow_rules: Sequence[AllowRule] = ()
    grants: list[Grant] = field(default_factory=list)
    consumed: list[Grant] = field(default_factory=list)

    def _grant(self, req: ApprovalRequest) -> Grant | None:
        for g in self.grants:
            if g.call_id == req.call_id and g.tool == req.tool and \
                    g.arguments_digest == req.arguments_digest:
                self.grants.remove(g)
                self.consumed.append(g)
                return g
        return None

    async def resolve(self, req: ApprovalRequest) -> Resolution:
        if self.mode is ApprovalMode.DONT_ASK:
            return Resolution(False, "mode:dont_ask",
                              "unattended run: every ask becomes a deny",
                              (Step("mode:dont_ask", Verdict.REJECT, "unattended"),))
        grant = self._grant(req)
        if grant is not None:
            return Resolution(True, grant.approver, grant.reason or "pre-issued grant",
                              (Step(grant.approver, Verdict.APPROVE, "grant consumed"),))
        if not req.bypass_immune:
            for rule in self.allow_rules:
                if rule.matches(req):
                    return Resolution(True, f"rule:{rule.name}", "allowed by rule",
                                      (Step(f"rule:{rule.name}", Verdict.APPROVE, "rule"),))
        steps: list[Step] = []
        for approver in self.approvers:
            d = await approver(req)
            mismatch = [what for what, mine, theirs in (
                ("call_id", req.call_id, d.call_id), ("tool", req.tool, d.tool),
                ("arguments_digest", req.arguments_digest, d.arguments_digest))
                if theirs is not None and theirs != mine]
            if mismatch:
                steps.append(Step(d.approver, Verdict.REJECT,
                                  f"decision bound to a different {', '.join(mismatch)}"))
                return Resolution(False, d.approver, steps[-1].reason, tuple(steps))
            steps.append(Step(d.approver, d.outcome, d.reason))
            if d.outcome is Verdict.APPROVE:
                return Resolution(True, d.approver, d.reason, tuple(steps), d.edited_arguments)
            if d.outcome is Verdict.REJECT:
                return Resolution(False, d.approver, d.reason, tuple(steps))
        steps.append(Step("policy:default-reject", Verdict.REJECT, "no approver decided"))
        return Resolution(False, "policy:default-reject", "no approver decided", tuple(steps))


__all__ = ["BYPASS_IMMUNE_EFFECTS", "AllowRule", "ApprovalGate", "ApprovalRequest",
           "Approver", "Decision", "Grant", "Resolution", "Step", "Verdict", "deny_all"]
