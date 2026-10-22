"""Risk analysis, kept separate from the confirmation decision (pattern from OpenHands
software-agent-sdk ``security/``, MIT; no code copied).

Analyzers each give a tool call a :class:`~agentic_robotics.work.contracts.Risk`
(LOW / MEDIUM / HIGH / UNKNOWN) with a reason. :func:`combine` takes the worst: HIGH if any
analyzer says HIGH, otherwise UNKNOWN if any is unsure, otherwise the highest known level.
:class:`ConfirmRisky` then decides whether to ask: at or above a threshold (which may not
be UNKNOWN), and on UNKNOWN by default. An analyzer that cannot inspect a call must
answer UNKNOWN, never a silent LOW.

Risk only ever *adds* asks. A tool's declared side effect remains the floor: effects in
the task's ``approval_required`` are asked for whatever the analyzers say.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..core.errors import FrameworkError
from .contracts import Risk, SideEffect
from .execpolicy import CommandDecision, CommandEvaluation

__requirements__ = ("AIR01", "REQ02", "REQ04", "G15")

RANK = {Risk.LOW: 0, Risk.MEDIUM: 1, Risk.HIGH: 2}


@dataclass(frozen=True)
class CallView:
    """What analyzers see of a tool call (arguments are data, never instructions)."""

    tool: str
    side_effect: SideEffect
    arguments: Mapping[str, Any]
    unparseable: bool = False
    command: CommandEvaluation | None = None


@dataclass(frozen=True)
class RiskAssessment:
    risk: Risk
    analyzer: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"risk": self.risk.value, "analyzer": self.analyzer, "reason": self.reason}


class RiskAnalyzer(Protocol):
    @property
    def name(self) -> str: ...

    def analyze(self, call: CallView) -> RiskAssessment: ...


def combine(assessments: Iterable[RiskAssessment]) -> Risk:
    risks = [a.risk for a in assessments]
    if not risks:
        return Risk.UNKNOWN
    if Risk.HIGH in risks:
        return Risk.HIGH
    if Risk.UNKNOWN in risks:
        return Risk.UNKNOWN
    return max(risks, key=RANK.__getitem__)


@dataclass(frozen=True)
class ConfirmRisky:
    threshold: Risk = Risk.HIGH
    confirm_unknown: bool = True

    def __post_init__(self) -> None:
        if self.threshold is Risk.UNKNOWN:
            raise FrameworkError("the confirmation threshold cannot be UNKNOWN")

    def should_confirm(self, risk: Risk) -> bool:
        if risk is Risk.UNKNOWN:
            return self.confirm_unknown
        return RANK[risk] >= RANK[self.threshold]


SIDE_EFFECT_RISK = {SideEffect.READ: Risk.LOW, SideEffect.WORKSPACE: Risk.LOW,
                    SideEffect.COMPUTE: Risk.MEDIUM, SideEffect.PROJECT: Risk.LOW,
                    SideEffect.EXTERNAL: Risk.HIGH, SideEffect.ROBOT: Risk.HIGH,
                    SideEffect.PROMOTE: Risk.HIGH}


class SideEffectAnalyzer:
    name = "side_effect"

    def analyze(self, call: CallView) -> RiskAssessment:
        if call.unparseable:
            return RiskAssessment(Risk.UNKNOWN, self.name, "arguments could not be parsed")
        risk = SIDE_EFFECT_RISK[call.side_effect]
        return RiskAssessment(risk, self.name, f"declared side effect '{call.side_effect}'")


class PolicyAnalyzer:
    """Maps the command policy decision onto risk: forbidden HIGH, prompt MEDIUM."""

    name = "command_policy"

    def analyze(self, call: CallView) -> RiskAssessment:
        if call.command is None:
            return RiskAssessment(Risk.LOW, self.name, "no command to evaluate")
        d = call.command.decision
        risk = {CommandDecision.ALLOW: Risk.LOW, CommandDecision.PROMPT: Risk.MEDIUM,
                CommandDecision.FORBIDDEN: Risk.HIGH}[d]
        return RiskAssessment(risk, self.name, f"command policy: {d.value}")


@dataclass(frozen=True)
class RiskPattern:
    regex: str
    risk: Risk
    reason: str


DEFAULT_PATTERNS: tuple[RiskPattern, ...] = (
    RiskPattern(r"\brm\s+-\w*[rR]\w*\s+(/|~|\$HOME)(\s|$)", Risk.HIGH,
                "recursive delete of a root or home directory"),
    RiskPattern(r"\bsudo\b|\bdoas\b|\bsu\s+-", Risk.HIGH, "privilege escalation"),
    RiskPattern(r"(curl|wget)[^|;\n]*\|\s*(ba|z)?sh\b", Risk.HIGH, "pipes a download into a shell"),
    RiskPattern(r"169\.254\.169\.254|metadata\.google\.internal|fd00:ec2::254", Risk.HIGH,
                "cloud metadata endpoint"),
    RiskPattern(r"/etc/(shadow|sudoers)|\.ssh/(id_|authorized_keys)|\.aws/credentials",
                Risk.HIGH, "credential or system secret files"),
    RiskPattern(r"\.git/(hooks|config)\b", Risk.HIGH, "repository hooks or config"),
    RiskPattern(r"\bbase64\s+(-d|--decode)\b.*\|\s*(ba)?sh", Risk.HIGH, "decoded payload to shell"),
    RiskPattern(r"\b(os\.system|subprocess\.|socket\.socket|ctypes\.|__import__\()", Risk.MEDIUM,
                "process, socket or native access from code"),
    RiskPattern(r"(?i)\b(api[_-]?key|secret|passw(or)?d|token)\s*[=:]", Risk.MEDIUM,
                "looks like a credential assignment"),
)


def _strings(value: Any, depth: int = 0) -> list[str]:
    if depth > 16:
        raise ValueError("arguments nest too deeply to inspect")
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for k, v in value.items() for s in [str(k), *_strings(v, depth + 1)]]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v, depth + 1)]
    return [json.dumps(value)]


@dataclass(frozen=True)
class PatternAnalyzer:
    patterns: Sequence[RiskPattern] = DEFAULT_PATTERNS
    name: str = "pattern"
    _compiled: tuple[tuple[re.Pattern[str], RiskPattern], ...] = field(init=False, repr=False,
                                                                       compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled",
                           tuple((re.compile(p.regex), p) for p in self.patterns))

    def analyze(self, call: CallView) -> RiskAssessment:
        if call.unparseable:
            return RiskAssessment(Risk.UNKNOWN, self.name, "arguments could not be parsed")
        try:
            text = "\n".join(_strings(call.arguments))
        except ValueError as exc:
            return RiskAssessment(Risk.UNKNOWN, self.name, str(exc))
        hits = [p for rx, p in self._compiled if rx.search(text)]
        if not hits:
            return RiskAssessment(Risk.LOW, self.name, "no risky pattern")
        worst = max(hits, key=lambda p: RANK.get(p.risk, 3))
        return RiskAssessment(worst.risk, self.name, "; ".join(h.reason for h in hits))


DEFAULT_ANALYZERS: tuple[RiskAnalyzer, ...] = (SideEffectAnalyzer(), PolicyAnalyzer(),
                                               PatternAnalyzer())


def assess(call: CallView, analyzers: Iterable[RiskAnalyzer] = DEFAULT_ANALYZERS
           ) -> tuple[Risk, list[RiskAssessment]]:
    out = []
    for a in analyzers:
        try:
            out.append(a.analyze(call))
        except Exception as exc:
            out.append(RiskAssessment(Risk.UNKNOWN, getattr(a, "name", "analyzer"),
                                      f"analyzer failed: {type(exc).__name__}"))
    return combine(out), out


__all__ = ["DEFAULT_ANALYZERS", "DEFAULT_PATTERNS", "CallView", "ConfirmRisky",
           "PatternAnalyzer", "PolicyAnalyzer", "RiskAnalyzer", "RiskAssessment",
           "RiskPattern", "SideEffectAnalyzer", "assess", "combine"]
