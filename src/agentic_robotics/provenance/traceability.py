"""Requirements traceability: requirement -> implementing code -> test evidence ->
recorded activities, with a check that every *claimed* status is backed by evidence.

Code declares what it implements with a module-level ``__requirements__ = ("REQ09",)``
or the :func:`implements` decorator; tests declare what they evidence with
``@pytest.mark.req("REQ09", "G06")``. Code is scanned statically (never imported).
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

from .model import RequirementKind, RequirementSet, RequirementStatus

__requirements__ = ("REQ02", "REQ18")

F = TypeVar("F", bound=Callable[..., Any])


def implements(*req_ids: str) -> Callable[[F], F]:
    """Mark a function or class as implementing requirements. No runtime effect."""

    def wrap(obj: F) -> F:
        existing = tuple(getattr(obj, "__requirements__", ()))
        obj.__requirements__ = existing + req_ids  # type: ignore[attr-defined]
        return obj

    return wrap


def baseline_requirements() -> RequirementSet:
    text = resources.files(__package__).joinpath("baseline_requirements.json").read_text(
        "utf-8")
    return RequirementSet.from_json(text)


def scan_code(roots: Iterable[Path], base: Path) -> dict[str, list[str]]:
    """Map requirement id -> ``module`` or ``module:object`` locations that claim it."""
    index: dict[str, list[str]] = {}
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in files:
            try:
                tree = ast.parse(path.read_text("utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            try:
                rel = path.resolve().relative_to(base.resolve())
            except ValueError:
                rel = path
            module = rel.with_suffix("").as_posix().replace("/", ".").removeprefix("src.")
            module = module.removesuffix(".__init__")
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "__requirements__"
                        for t in node.targets):
                    for rid in _strings(node.value):
                        index.setdefault(rid, []).append(module)
            for sub in ast.walk(tree):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    for dec in sub.decorator_list:
                        if isinstance(dec, ast.Call) and _name(dec.func) == "implements":
                            for arg in dec.args:
                                for rid in _strings(arg):
                                    index.setdefault(rid, []).append(f"{module}:{sub.name}")
    return index


def _strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [e.value for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
    return []


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


@dataclass
class TestEvidence:
    __test__ = False  # not a pytest test class

    tests: list[dict[str, Any]] = field(default_factory=list)
    run: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> TestEvidence:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text("utf-8"))
        return cls(tests=list(data.get("tests", [])), run=dict(data.get("run", {})))

    def by_requirement(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for t in self.tests:
            for rid in t.get("requirements", []):
                out.setdefault(rid, []).append(t)
        return out


class Computed:
    """What the evidence shows. ``evidenced`` means code (for requirements) and passing
    tagged tests exist; it does not by itself mean the requirement is complete. That is
    what the separately *claimed* status says, and the check holds claims to evidence."""

    VERIFIED = "evidenced"
    IMPLEMENTED = "implemented"
    TESTED_ONLY = "tested_only"
    FAILING = "failing"
    UNTRACED = "untraced"


@dataclass
class MatrixRow:
    req_id: str
    title: str
    kind: str
    claimed: str
    computed: str
    modules: list[str] = field(default_factory=list)
    tests_passed: list[str] = field(default_factory=list)
    tests_failed: list[str] = field(default_factory=list)
    activities: int = 0
    traces_to: list[str] = field(default_factory=list)


def build_matrix(requirements: RequirementSet, code: dict[str, list[str]],
                 evidence: TestEvidence,
                 activity_counts: dict[str, int] | None = None) -> list[MatrixRow]:
    by_req = evidence.by_requirement()
    rows = []
    for r in requirements.requirements:
        tests = by_req.get(r.req_id, [])
        passed = sorted(t["nodeid"] for t in tests if t["outcome"] == "passed")
        failed = sorted(t["nodeid"] for t in tests if t["outcome"] in ("failed", "error"))
        modules = sorted(set(code.get(r.req_id, [])))
        if failed:
            computed = Computed.FAILING
        elif r.kind is RequirementKind.GATE:
            # Gates are demonstrations: evidenced by tests, not by code claims.
            computed = Computed.VERIFIED if passed else Computed.UNTRACED
        elif modules and passed:
            computed = Computed.VERIFIED
        elif modules:
            computed = Computed.IMPLEMENTED
        elif passed:
            computed = Computed.TESTED_ONLY
        else:
            computed = Computed.UNTRACED
        rows.append(MatrixRow(r.req_id, r.title, r.kind.value, r.status.value, computed,
                              modules, passed, failed,
                              (activity_counts or {}).get(r.req_id, 0), list(r.traces_to)))
    return rows


def check(rows: list[MatrixRow], requirements: RequirementSet, code: dict[str, list[str]],
          evidence: TestEvidence) -> list[str]:
    """Problems that make a claimed status untrue, or references to unknown ids."""
    problems = []
    for row in rows:
        if row.computed == Computed.FAILING:
            problems.append(f"{row.req_id}: failing tests {row.tests_failed}")
        if row.claimed == RequirementStatus.VERIFIED and row.computed != Computed.VERIFIED:
            problems.append(f"{row.req_id}: claimed verified, evidence is {row.computed}")
        if row.claimed == RequirementStatus.IMPLEMENTED and row.computed in (
                Computed.UNTRACED, Computed.TESTED_ONLY) and row.kind != "gate":
            problems.append(f"{row.req_id}: claimed implemented, no code declares it")
    known = {r.req_id for r in requirements.requirements}
    for rid, where in sorted(code.items()):
        if rid not in known:
            problems.append(f"unknown requirement {rid!r} declared in {sorted(set(where))}")
    for rid, tests in sorted(evidence.by_requirement().items()):
        if rid not in known:
            problems.append(f"unknown requirement {rid!r} cited by {len(tests)} test(s)")
    return problems


_ICON = {Computed.VERIFIED: "evidenced", Computed.IMPLEMENTED: "code only",
         Computed.TESTED_ONLY: "tested only", Computed.FAILING: "FAILING",
         Computed.UNTRACED: "-"}


def render_markdown(rows: list[MatrixRow], kinds: Iterable[str] | None = None) -> str:
    wanted = set(kinds) if kinds else None
    lines = ["| ID | Title | Claimed | Evidence | Code | Tests | Activities |",
             "|---|---|---|---|---|---|---|"]
    for r in rows:
        if wanted and r.kind not in wanted:
            continue
        tests = f"{len(r.tests_passed)} pass" + (f", {len(r.tests_failed)} fail"
                                                  if r.tests_failed else "")
        code = ", ".join(".".join(m.split(":")[0].split(".")[-2:]) for m in r.modules[:4]) + (
            " ..." if len(r.modules) > 4 else "")
        lines.append(f"| {r.req_id} | {r.title} | {r.claimed} | {_ICON[r.computed]} | "
                     f"{code or '-'} | {tests if (r.tests_passed or r.tests_failed) else '-'}"
                     f" | {r.activities or '-'} |")
    return "\n".join(lines) + "\n"


def rows_to_json(rows: list[MatrixRow]) -> str:
    return json.dumps([asdict(r) for r in rows], indent=2)
