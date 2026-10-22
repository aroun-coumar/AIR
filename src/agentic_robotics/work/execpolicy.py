"""Command policy rules for the ``run`` tool (pattern from openai/codex execpolicy,
Apache-2.0; no code copied).

A rule is an ordered token *prefix*; each element is a token or a list of alternatives.
Its decision is ``allow``, ``prompt`` (ask for approval) or ``forbidden``, with a
justification shown to the model and the approver (for ``forbidden``, name the
alternative). Each rule carries ``match`` / ``not_match`` examples that are checked when
the policy loads, so a rule that does not do what it says fails loudly, like a unit test.
Across all matching rules the **strictest** decision wins (forbidden > prompt > allow).
A command no rule matches gets the policy's ``default`` decision; the default ``allow``
means "fall back to the sandbox", whose command allowlist still applies.

    {"$schema": "ar.command_policy/1.0",
     "rules": [{"pattern": ["git", "push"], "decision": "forbidden",
                "justification": "Agents do not publish; submit a patch with `propose`.",
                "match": ["git push origin main"], "not_match": ["git status"]}]}
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePath
from typing import Any

from ..core.errors import FrameworkError
from ..core.schema import Contract, contract

__requirements__ = ("AIR01", "REQ02", "REQ04", "G15")


class CommandPolicyError(FrameworkError):
    code = "command_policy_error"


class CommandDecision(StrEnum):
    ALLOW = "allow"
    PROMPT = "prompt"
    FORBIDDEN = "forbidden"


SEVERITY = {CommandDecision.ALLOW: 0, CommandDecision.PROMPT: 1, CommandDecision.FORBIDDEN: 2}

Token = str | tuple[str, ...]
Example = str | tuple[str, ...]


def program_name(token: str) -> str:
    """Basename of a program token, case-folded, without a Windows executable suffix."""
    name = PurePath(token.replace("\\", "/")).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def tokens(example: Example | Sequence[str]) -> list[str]:
    return shlex.split(example) if isinstance(example, str) else list(example)


@dataclass(frozen=True)
class CommandRule:
    pattern: tuple[Token, ...]
    decision: CommandDecision = CommandDecision.ALLOW
    justification: str = ""
    match: tuple[Example, ...] = ()
    not_match: tuple[Example, ...] = ()

    def matches(self, argv: Sequence[str]) -> bool:
        if not self.pattern or len(argv) < len(self.pattern):
            return False
        for i, (want, have) in enumerate(zip(self.pattern, argv, strict=False)):
            options = (want,) if isinstance(want, str) else want
            if i == 0:
                if have not in options and program_name(have) not in {
                        program_name(o) for o in options}:
                    return False
            elif have not in options:
                return False
        return True

    def describe(self) -> str:
        return " ".join(t if isinstance(t, str) else "[" + "|".join(t) + "]"
                        for t in self.pattern)


@dataclass(frozen=True)
class RuleMatch:
    rule: CommandRule
    matched_prefix: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"prefixRuleMatch": {"matchedPrefix": list(self.matched_prefix),
                                    "decision": self.rule.decision.value,
                                    "justification": self.rule.justification}}


@dataclass(frozen=True)
class CommandEvaluation:
    argv: tuple[str, ...]
    decision: CommandDecision
    matched: tuple[RuleMatch, ...]
    defaulted: bool

    @property
    def justification(self) -> str:
        strictest = [m.rule.justification for m in self.matched
                     if m.rule.decision is self.decision and m.rule.justification]
        return "; ".join(strictest) or ("no rule matched; policy default"
                                        if self.defaulted else "")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"matchedRules": [m.to_dict() for m in self.matched],
                               "decision": self.decision.value}
        if self.defaulted:
            out["defaulted"] = True
        return out


@contract("command_policy", version="1.0")
class CommandPolicy(Contract):
    """Prefix rules (allow / prompt / forbidden) for commands a work agent runs; the
    strictest matching rule wins; examples are verified at load time."""

    rules: tuple[CommandRule, ...] = ()
    default: CommandDecision = CommandDecision.ALLOW

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise CommandPolicyError("command policy examples do not hold", problems=problems)

    def problems(self) -> list[str]:
        out = []
        for i, rule in enumerate(self.rules):
            if not rule.pattern:
                out.append(f"rule {i}: empty pattern")
                continue
            for ex in rule.match:
                if not rule.matches(tokens(ex)):
                    out.append(f"rule {i} ({rule.describe()}): example {ex!r} should match")
            for ex in rule.not_match:
                if rule.matches(tokens(ex)):
                    out.append(f"rule {i} ({rule.describe()}): example {ex!r} must not match")
        return out

    def evaluate(self, argv: Sequence[str]) -> CommandEvaluation:
        argv = tuple(argv)
        matched = tuple(RuleMatch(r, argv[: len(r.pattern)]) for r in self.rules
                        if r.matches(argv))
        if not matched:
            return CommandEvaluation(argv, self.default, (), True)
        decision = max((m.rule.decision for m in matched), key=SEVERITY.__getitem__)
        return CommandEvaluation(argv, decision, matched, False)

    def merged(self, *others: CommandPolicy | None) -> CommandPolicy:
        """All rules of this and ``others``; the strictest default wins."""
        rules = list(self.rules)
        default = self.default
        for o in others:
            if o is None:
                continue
            rules.extend(o.rules)
            default = max(default, o.default, key=SEVERITY.__getitem__)
        return CommandPolicy(rules=tuple(rules), default=default)


def _rule(pattern: Iterable[Token], decision: CommandDecision, justification: str,
          match: Iterable[Example], not_match: Iterable[Example]) -> CommandRule:
    return CommandRule(tuple(pattern), decision, justification, tuple(match), tuple(not_match))


PY = ("python", "python3")
#: Built-in rules every work agent applies; a task's own rules are added to these, and as
#: the strictest decision wins, a task can tighten them but never loosen them.
DEFAULT_COMMAND_POLICY = CommandPolicy(rules=(
    _rule(["git", "push"], CommandDecision.FORBIDDEN,
          "Agents do not publish changes; submit a patch with `propose` instead.",
          ["git push origin main", "git push"], ["git status", "git pull"]),
    _rule([PY, "-m", "pip", ("install", "uninstall", "download")], CommandDecision.FORBIDDEN,
          "Installing packages changes the environment; list the dependency in a proposal.",
          ["python -m pip install requests", "python3 -m pip uninstall x"],
          ["python -m pytest", "python -m pip list"]),
    _rule([("pip", "pip3", "uv", "conda"), ("install", "uninstall", "add", "remove")],
          CommandDecision.FORBIDDEN,
          "Installing packages changes the environment; list the dependency in a proposal.",
          ["pip install numpy", "uv add httpx"], ["pip list", "uv run x"]),
    _rule([("curl", "wget", "ssh", "scp", "nc", "ncat")], CommandDecision.PROMPT,
          "Network clients reach outside the sandbox; a person must approve.",
          ["curl https://example.com", "ssh host"], ["python fetch.py"]),
    _rule(["git", ("reset", "clean", "checkout", "rebase", "filter-branch")],
          CommandDecision.PROMPT, "Rewrites or discards work in the repository.",
          ["git reset --hard", "git clean -fdx"], ["git log", "git diff"]),
))


__all__ = ["DEFAULT_COMMAND_POLICY", "CommandDecision", "CommandEvaluation", "CommandPolicy",
           "CommandPolicyError", "CommandRule", "RuleMatch", "program_name", "tokens"]
