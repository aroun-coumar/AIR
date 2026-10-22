"""Reproducibility lock (the ``dvc.lock`` pattern): what an activity's result depended on,
resolved, so that later one can ask "has anything changed?".

``.air/repro.lock`` holds one entry per locked activity: its parameters, the SHA-256 of
every input file it used (and every output it generated), and the environment: Python,
platform, installed package versions, git commit and dirty state. ``check`` recomputes
all of it and lists each difference. A lock is a statement of *what to compare*, not a
claim that a re-run would give identical results; determinism is measured elsewhere.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.errors import SchemaError

if TYPE_CHECKING:
    from .project import Project

__requirements__ = ("F05", "REQ02")

LOCK_FORMAT = "air.repro_lock/1"
LOCK_FILE = "repro.lock"


def installed_packages() -> dict[str, str]:
    out: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name:
            out[str(name).lower().replace("_", "-")] = str(dist.version)
    return dict(sorted(out.items()))


def environment(root: Path) -> dict[str, Any]:
    from .project import capture_environment

    env = capture_environment(root)
    return {"python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(), "machine": platform.machine(),
            "executable_prefix_is_venv": sys.prefix != sys.base_prefix,
            "framework_version": env.get("framework_version"),
            "git": env.get("git"), "packages": installed_packages()}


def _files(project: Project, entity_ids: Any) -> dict[str, dict[str, Any]]:
    ents = project.entities()
    out: dict[str, dict[str, Any]] = {}
    for eid in entity_ids:
        e = ents.get(eid)
        if e is None or not e.digest:
            continue
        out[eid] = {"uri": e.uri, "digest": e.digest, "size_bytes": e.size_bytes}
    return out


def lock_activity(project: Project, activity_id: str | None = None, *,
                  env: dict[str, Any] | None = None) -> dict[str, Any]:
    """Add (or refresh) the lock entry for an activity (default: the latest finished)."""
    acts = [a for a in project.activities() if a.ended_at is not None]
    if not acts:
        raise SchemaError("no finished activity to lock")
    act = next((a for a in acts if a.activity_id == activity_id), None) if activity_id \
        else max(acts, key=lambda a: a.ended_at or 0)
    if act is None:
        raise SchemaError("unknown or unfinished activity", activity_id=activity_id)
    entry = {"title": act.title, "kind": act.kind.value, "status": act.status.value,
             "params": act.params, "inputs": _files(project, act.used),
             "outputs": _files(project, act.generated),
             "environment": env if env is not None else environment(project.root)}
    path = project.air_dir / LOCK_FILE
    doc = read_lock(project) or {"format": LOCK_FORMAT, "activities": {}}
    doc["activities"][act.activity_id] = entry
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8",
                    newline="\n")
    from .project import file_digest

    project.ledger.append("repro.locked", act.activity_id,
                          {"lock_digest": file_digest(path), "inputs": len(entry["inputs"]),
                           "outputs": len(entry["outputs"])})
    return {"activity_id": act.activity_id, **entry}


def read_lock(project: Project) -> dict[str, Any] | None:
    path = project.air_dir / LOCK_FILE
    if not path.exists():
        return None
    doc = json.loads(path.read_text("utf-8"))
    if doc.get("format") != LOCK_FORMAT:
        raise SchemaError("unsupported repro lock format", format=doc.get("format"))
    return dict(doc)


@dataclass
class Change:
    activity_id: str
    what: str
    detail: str

    def __str__(self) -> str:
        return f"{self.activity_id}: {self.what}: {self.detail}"


@dataclass
class ReproReport:
    locked: int
    changes: list[Change] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.changes


def check(project: Project, *, env: dict[str, Any] | None = None,
          activity_id: str | None = None) -> ReproReport:
    from .project import file_digest

    doc = read_lock(project)
    if doc is None:
        raise SchemaError("no .air/repro.lock (run `air repro lock` first)")
    current = env if env is not None else environment(project.root)
    items = {k: v for k, v in doc["activities"].items()
             if activity_id is None or k == activity_id}
    report = ReproReport(len(items))
    for aid, entry in sorted(items.items()):
        def add(what: str, detail: str, aid: str = aid) -> None:
            report.changes.append(Change(aid, what, detail))

        for group in ("inputs", "outputs"):
            for eid, f in entry[group].items():
                uri = f.get("uri")
                if not uri:  # an in-memory blob: its digest is all there is to compare
                    continue
                p = project.root / uri
                if not p.is_file():
                    add(f"{group[:-1]} missing", f"{eid} ({uri})")
                elif file_digest(p) != f["digest"]:
                    add(f"{group[:-1]} changed", f"{uri}: {f['digest'][:19]}... -> "
                                                 f"{file_digest(p)[:19]}...")
        old = entry["environment"]
        for key in ("python", "implementation", "platform", "machine", "framework_version"):
            if old.get(key) != current.get(key):
                add("environment", f"{key}: {old.get(key)} -> {current.get(key)}")
        og, cg = old.get("git") or {}, current.get("git") or {}
        if og.get("commit") != cg.get("commit"):
            add("git commit", f"{str(og.get('commit'))[:12]} -> {str(cg.get('commit'))[:12]}")
        if cg.get("dirty") and not og.get("dirty"):
            add("git", "working tree now has uncommitted changes")
        op, cp = old.get("packages") or {}, current.get("packages") or {}
        for name in sorted(set(op) | set(cp)):
            if op.get(name) != cp.get(name):
                add("package", f"{name}: {op.get(name, 'absent')} -> {cp.get(name, 'absent')}")
    return report
