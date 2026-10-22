"""Context feeder: a generated, always-current briefing on a project for the people and
AI agents working on it. It is rebuilt from the repository and its ledger, so it does not
go stale as the repository grows.

    air context --out docs/CONTEXT.md
"""

from __future__ import annotations

import ast
import datetime as dt
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from .model import RequirementStatus
from .project import Project
from .traceability import Computed, TestEvidence, build_matrix, scan_code

__requirements__ = ("REQ08", "REQ18")

SKIP_DIRS = {".venv", "venv", ".git", "__pycache__", ".air", "node_modules", "build", "dist",
             ".mypy_cache", ".ruff_cache", ".pytest_cache"}


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if not git:
        return ""
    try:
        out = subprocess.run([git, *args], cwd=root, capture_output=True, text=True,
                             timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _first_line(doc: str | None) -> str:
    return (doc or "").strip().splitlines()[0].strip() if doc and doc.strip() else ""


def _source_roots(root: Path, package: str | None) -> list[Path]:
    if package:
        for cand in (root / "src" / package, root / package):
            if cand.is_dir():
                return [cand]
    src = root / "src"
    return [src] if src.is_dir() else [root]


def _py_files(base: Path) -> list[Path]:
    return sorted(p for p in base.rglob("*.py")
                  if not any(part in SKIP_DIRS for part in p.parts))


def package_map(root: Path, package: str | None) -> list[str]:
    lines = []
    for base in _source_roots(root, package):
        for path in _py_files(base):
            try:
                tree = ast.parse(path.read_text("utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            rel = path.relative_to(root).as_posix()
            reqs: list[str] = []
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "__requirements__"
                        for t in node.targets) and isinstance(node.value, (ast.Tuple, ast.List)):
                    reqs = [e.value for e in node.value.elts
                            if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            summary = _first_line(ast.get_docstring(tree)) or "(no module docstring)"
            tag = f" [{', '.join(reqs)}]" if reqs else ""
            lines.append(f"- `{rel}`: {summary}{tag}")
    return lines


def contract_map(root: Path, package: str | None) -> list[str]:
    """Find ``@contract("name", version=...)`` classes statically, with their fields."""
    lines = []
    for base in _source_roots(root, package):
        for path in _py_files(base):
            try:
                tree = ast.parse(path.read_text("utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for dec in node.decorator_list:
                    if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name)
                            and dec.func.id == "contract" and dec.args
                            and isinstance(dec.args[0], ast.Constant)):
                        continue
                    schema = str(dec.args[0].value)
                    version = str(next((k.value.value for k in dec.keywords
                                        if k.arg == "version"
                                        and isinstance(k.value, ast.Constant)), "1.0"))
                    fields = [s.target.id for s in node.body
                              if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)
                              and not s.target.id.isupper()]
                    lines.append(f"- `{schema}/{version}` ({node.name}): "
                                 + ", ".join(fields))
    return sorted(lines)


def decisions(root: Path) -> list[str]:
    d = root / "docs" / "decisions"
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.md")):
        text = p.read_text("utf-8").splitlines()
        title = next((ln.lstrip("# ").strip() for ln in text if ln.startswith("#")), p.stem)
        status = next((ln.split(":", 1)[1].strip() for ln in text
                       if ln.lower().startswith("status:")), "unknown")
        out.append(f"- [{title}]({p.relative_to(root).as_posix()}): {status}")
    return out


def build_context_pack(root: str | Path = ".", *, package: str | None = None,
                       evidence: str | Path | None = None, activities: int = 15,
                       commits: int = 10) -> str:
    root = Path(root).resolve()
    pyproject: dict[str, Any] = {}
    if (root / "pyproject.toml").exists():
        pyproject = tomllib.loads((root / "pyproject.toml").read_text("utf-8")).get(
            "project", {})
    name = pyproject.get("name", root.name)
    package = package or (name.replace("-", "_") if pyproject else None)
    commit = _git(root, "rev-parse", "--short", "HEAD") or "uncommitted"
    now = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")

    out = [f"# Context pack: {name}", "",
           f"Generated {now} at commit `{commit}` by `air context`. Do not edit by hand; "
           "regenerate it.", ""]
    if pyproject:
        deps = ", ".join(pyproject.get("dependencies", [])) or "none"
        out += ["## Project", "",
                f"- **Version:** {pyproject.get('version', '?')}",
                f"- **Description:** {pyproject.get('description', '')}",
                f"- **Python:** {pyproject.get('requires-python', '?')}",
                f"- **Runtime dependencies:** {deps}", ""]
    guides = [p for p in ("CLAUDE.md", "README.md", "docs/architecture.md")
              if (root / p).exists()]
    if guides:
        out += ["## Read first", ""] + [f"- [{g}]({g})" for g in guides] + [""]

    project: Project | None
    try:
        project = Project.open(root)
    except Exception:
        project = None

    reqs = project.requirements if project else None
    if reqs is not None:
        code = scan_code(_source_roots(root, package), root)
        ev_path = Path(evidence) if evidence else (root / ".air" / "test-evidence.json")
        rows = build_matrix(reqs, code, TestEvidence.load(ev_path))
        counts: dict[str, int] = {}
        for r in rows:
            if r.kind in ("requirement", "gate"):
                counts[r.computed] = counts.get(r.computed, 0) + 1
        out += ["## Requirements status", "",
                "Evidence status for REQ and gate items: " + ", ".join(
                    f"{k} {v}" for k, v in sorted(counts.items())), ""]
        open_items = [r for r in rows if r.kind in ("requirement", "gate")
                      and r.claimed != RequirementStatus.VERIFIED]
        if open_items:
            out += ["Open (not yet claimed verified):", ""]
            out += [f"- {r.req_id} {r.title}: claimed {r.claimed}, evidence {r.computed}"
                    for r in open_items]
            out.append("")
        failing = [r for r in rows if r.computed == Computed.FAILING]
        if failing:
            out += ["**Failing:** " + ", ".join(r.req_id for r in failing), ""]

    pm = package_map(root, package)
    if pm:
        out += ["## Package map", "", *pm, ""]
    cm = contract_map(root, package)
    if cm:
        out += ["## Contracts (schema/version: fields)", "", *cm, ""]
    dm = decisions(root)
    if dm:
        out += ["## Decisions", "", *dm, ""]

    if project is not None:
        acts = project.activities()[-activities:]
        if acts:
            out += ["## Recent recorded activities", ""]
            for a in reversed(acts):
                reqs_s = f" [{', '.join(a.requirements)}]" if a.requirements else ""
                out.append(f"- {dt.datetime.fromtimestamp(a.started_at, dt.UTC):%Y-%m-%d} "
                           f"{a.kind.value}: {a.title} ({a.status.value}){reqs_s}")
            out.append("")
        out += _triage_section(project)
        report = project.ledger.verify()
        out += ["## Ledger", "",
                f"- entries: {report.entries}, integrity: "
                f"{'OK' if report.ok else 'BROKEN at ' + str(report.first_bad_seq)}",
                f"- head: `{report.head[:16]}`", ""]

    log = _git(root, "log", f"-{commits}", "--format=- `%h` %s (%ad)", "--date=short")
    if log:
        out += ["## Recent commits", "", log, ""]
    return "\n".join(out)


def _triage_section(project: Project, limit: int = 15) -> list[str]:
    """Open triage items, most severe first (read only: no sweep is run here)."""
    from ..triage import TriageBook

    book = TriageBook(project)
    items = book.items()
    if not items:
        return []
    now = book.now()
    out = ["## Open triage", "",
           f"{len(items)} open item(s); run `air triage sweep` to refresh and "
           "`air triage list` for details.", ""]
    for i in items[:limit]:
        due = " **overdue**" if i.overdue(now) else ""
        out.append(f"- `{i.item_id}` {i.severity.value}/{i.priority.value} "
                   f"{i.state.value}, {i.assignee or i.owner}: {i.title} (x{i.count}){due}")
    if len(items) > limit:
        out.append(f"- ... and {len(items) - limit} more")
    return [*out, ""]
