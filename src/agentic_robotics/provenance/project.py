"""Project provenance: one place where every research, development, simulation,
evaluation and maintenance activity of a project is recorded with its inputs, outputs,
environment, requirements and causal trace.

    project = Project.open(".", create=True)
    with project.activity("simulation", "handoff with 200 ms delay",
                          requirements=["REQ12"], params={"delay_ms": 200}) as act:
        act.use("scenarios/handoff.json", kind="scenario")
        ...
        act.generate("results/run-17.json", kind="report")
        act.metric("success_rate", 0.98)

State lives in ``<project>/.air/``: a hash-chained ``ledger.jsonl``, a content-addressed
``artifacts/`` store, ``project.json`` and an optional ``requirements.json``.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from types import TracebackType
from typing import Any

from ..core.errors import SchemaError
from ..core.ids import new_id
from . import tracing
from .ledger import Ledger, LedgerEntry
from .model import (
    Activity,
    ActivityKind,
    ActivityStatus,
    EntityRef,
    ExperimentRecord,
    MaintenanceRecord,
    RequirementSet,
)

__requirements__ = ("REQ02", "REQ10", "REQ15", "REQ16", "REQ18")

AIR_DIR = ".air"

# Runtime trace kinds copied into the project ledger by :meth:`Project.runtime_sink`.
# High-rate kinds such as ``progress`` stay in the runtime trace only.
DEFAULT_RUNTIME_KINDS = frozenset({
    "robot_registered", "submitted", "accepted", "rejected", "started", "adapter_reported",
    "verification", "verified", "failed", "cancelled", "unresolved", "reported_complete",
    "deadline_exceeded", "adapter_error", "reconcile_started", "interrupt_reported",
    "cancel_requested", "authority_granted", "authority_revoked",
    "authority_revoked_during_execution", "reservation_held", "reservation_released",
    "reservation_superseded", "reservation_expired", "plan_started", "plan_finished",
    "agent_decision", "admission_snapshot", "duplicate_ignored",
})


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def capture_environment(root: Path) -> dict[str, Any]:
    """What a later reader needs to interpret or reproduce an activity."""
    from .. import __version__

    env: dict[str, Any] = {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "framework_version": __version__,
        "argv0": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "",
    }
    git = shutil.which("git")
    if git:
        def run(*args: str) -> str | None:
            try:
                out = subprocess.run([git, *args], cwd=root, capture_output=True, text=True,
                                     timeout=5, check=False)
            except (OSError, subprocess.SubprocessError):
                return None
            return out.stdout.strip() if out.returncode == 0 else None

        commit = run("rev-parse", "HEAD")
        if commit:
            env["git"] = {"commit": commit, "branch": run("rev-parse", "--abbrev-ref", "HEAD"),
                          "dirty": bool(run("status", "--porcelain"))}
    return env


class ArtifactStore:
    """Content-addressed file store: identical content is stored once."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, digest: str) -> Path:
        algo, _, hexd = digest.partition(":")
        if algo != "sha256" or len(hexd) != 64:
            raise SchemaError("unsupported digest", digest=digest)
        return self.root / algo / hexd[:2] / hexd[2:]

    def put_file(self, path: Path) -> str:
        digest = file_digest(path)
        target = self._path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        return digest

    def put_bytes(self, data: bytes) -> str:
        digest = bytes_digest(data)
        target = self._path(digest)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return digest

    def path(self, digest: str) -> Path:
        p = self._path(digest)
        if not p.exists():
            raise SchemaError("artifact not in store", digest=digest)
        return p

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()


class Project:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.air_dir = root / AIR_DIR
        meta_path = self.air_dir / "project.json"
        if not meta_path.exists():
            raise SchemaError("not an AIR project (no .air/project.json)", root=str(root))
        self.meta: dict[str, Any] = json.loads(meta_path.read_text("utf-8"))
        self.ledger = Ledger(self.air_dir / "ledger.jsonl")
        self.artifacts = ArtifactStore(self.air_dir / "artifacts")

    # -- lifecycle -----------------------------------------------------------------
    @classmethod
    def init(cls, path: str | Path = ".", name: str | None = None,
             requirements: RequirementSet | None = None) -> Project:
        root = Path(path).resolve()
        air = root / AIR_DIR
        if (air / "project.json").exists():
            raise SchemaError("project already initialized", root=str(root))
        air.mkdir(parents=True, exist_ok=True)
        meta = {"name": name or root.name, "created_at": time.time(),
                "layout_version": 1}
        (air / "project.json").write_text(json.dumps(meta, indent=2) + "\n",
                                          encoding="utf-8", newline="\n")
        (air / ".gitignore").write_text("artifacts/\n*.trace.jsonl\ntest-evidence.json\n"
                                        "*.jsonl.lock\nexports/\n",
                                        encoding="utf-8", newline="\n")
        project = cls(root)
        project.ledger.append("project.created", project.name,
                              {"environment": capture_environment(root)})
        if requirements is not None:
            project.set_requirements(requirements)
        return project

    @classmethod
    def open(cls, path: str | Path = ".", create: bool = False) -> Project:
        """Open the project containing ``path`` (searching upward, like git)."""
        start = Path(path).resolve()
        for candidate in (start, *start.parents):
            if (candidate / AIR_DIR / "project.json").exists():
                return cls(candidate)
        if create:
            return cls.init(start)
        raise SchemaError("no AIR project found here or in any parent", path=str(start))

    @property
    def name(self) -> str:
        return str(self.meta.get("name", self.root.name))

    # -- requirements ----------------------------------------------------------------
    @property
    def requirements(self) -> RequirementSet | None:
        p = self.air_dir / "requirements.json"
        return RequirementSet.load(p) if p.exists() else None

    def set_requirements(self, requirements: RequirementSet) -> None:
        p = self.air_dir / "requirements.json"
        requirements.save(p)
        self.ledger.append("requirements.updated", requirements.name,
                           {"digest": file_digest(p), "count": len(requirements.requirements)})

    # -- entities --------------------------------------------------------------------
    def entity(self, source: str | Path | bytes | None = None, *, kind: str,
               name: str | None = None, version: str | None = None, store: bool = False,
               physical: bool = False, uri: str | None = None,
               attributes: dict[str, Any] | None = None) -> EntityRef:
        """Register a file, blob, or physical/logical thing and return its reference."""
        digest = size = None
        if isinstance(source, bytes):
            digest, size = bytes_digest(source), len(source)
            if store:
                self.artifacts.put_bytes(source)
        elif source is not None:
            p = Path(source)
            if not p.is_absolute():
                p = (self.root / p) if (self.root / p).exists() else p.resolve()
            if not p.is_file():
                raise SchemaError("entity source file not found", path=str(source))
            digest = self.artifacts.put_file(p) if store else file_digest(p)
            size = p.stat().st_size
            name = name or self._rel(p)
            uri = uri or self._rel(p)
        if not name:
            raise SchemaError("an entity needs a name or a source")
        tag = version or (digest.split(":")[1][:12] if digest else "unversioned")
        ref = EntityRef(entity_id=f"{kind}:{name}@{tag}", kind=kind, name=name,
                        version=version, digest=digest, uri=uri, size_bytes=size,
                        physical=physical, attributes=dict(attributes or {}))
        known = self.ledger.query(kind="entity.registered", subject=ref.entity_id)
        if not known:
            self.ledger.append("entity.registered", ref.entity_id, {"entity": ref.to_dict()})
        return ref

    def _rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return p.resolve().as_posix()

    def entities(self) -> dict[str, EntityRef]:
        return {e.subject: EntityRef.from_dict(e.data["entity"])
                for e in self.ledger.query(kind="entity.registered")}

    # -- activities ------------------------------------------------------------------
    def activity(self, kind: ActivityKind | str, title: str, *,
                 requirements: Iterable[str] = (), params: dict[str, Any] | None = None,
                 actors: Iterable[str] = (), parent: str | None = None) -> ActivityRecorder:
        return ActivityRecorder(self, ActivityKind(kind), title, tuple(requirements),
                                dict(params or {}), tuple(actors), parent)

    def activities(self, *, kind: ActivityKind | str | None = None,
                   requirement: str | None = None) -> list[Activity]:
        finished = {e.subject: Activity.from_dict(e.data["activity"])
                    for e in self.ledger.query(kind="activity.finished")}
        out = []
        for e in self.ledger.query(kind="activity.started"):
            act = finished.get(e.subject) or Activity(
                activity_id=e.subject, kind=ActivityKind(e.data["kind"]),
                title=e.data["title"], status=ActivityStatus.RUNNING,
                started_at=e.ts, requirements=tuple(e.data.get("requirements", ())),
                trace_id=e.trace_id)
            if kind is not None and act.kind != ActivityKind(kind):
                continue
            if requirement is not None and requirement not in act.requirements:
                continue
            out.append(act)
        return out

    def record_maintenance(self, record: MaintenanceRecord,
                           requirements: Sequence[str] = ("REQ16",)) -> Activity:
        """Record a service action, preserving component lineage (baseline section 16)."""
        with self.activity(ActivityKind.MAINTENANCE,
                           f"{record.maintenance_type} maintenance on {record.robot_id}",
                           requirements=requirements, actors=record.performed_by,
                           params={"record_id": record.record_id}) as act:
            for component in record.components:
                act.use(self.entity(kind="component", name=component, physical=True))
            for r in record.replacements:
                removed = self.entity(kind="component", name=r.removed, physical=True)
                installed = self.entity(kind="component", name=r.installed, physical=True)
                act.generate(installed)
                self.ledger.append("entity.replaced", removed.entity_id,
                                   {"installed": installed.entity_id, "reason": r.reason,
                                    "activity_id": act.activity_id})
            for item in record.calibration_invalidated:
                self.ledger.append("calibration.invalidated", item,
                                   {"activity_id": act.activity_id,
                                    "robot_id": record.robot_id, "reason": record.reason})
            act.generate(record.to_json().encode(), kind="maintenance_record",
                         name=record.record_id, store=True)
            for f in record.findings:
                act.note(f)
            act.metric("returned_to_service", record.returned_to_service)
            act.outcome = ("returned to service" if record.returned_to_service
                           else "not returned to service")
        assert act.result is not None
        return act.result

    def record_experiment(self, record: ExperimentRecord, *, used: Iterable[str | Path] = (),
                          generated: Iterable[str | Path] = ()) -> Activity:
        with self.activity(ActivityKind.EXPERIMENT, record.hypothesis,
                           requirements=record.requirement_ids,
                           params={"experiment_id": record.experiment_id,
                                   "scenario": record.scenario, "backend": record.backend,
                                   "backend_version": record.backend_version,
                                   "seed": record.seed}) as act:
            for u in used:
                act.use(u, kind="input")
            for g in generated:
                act.generate(g, kind="result")
            act.generate(record.to_json().encode(), kind="experiment_record",
                         name=record.experiment_id, store=True)
            for k, v in record.results.items():
                act.metric(k, v)
            act.outcome = {True: "accepted", False: "rejected", None: "inconclusive"}[
                record.accepted]
        assert act.result is not None
        return act.result

    # -- lineage ---------------------------------------------------------------------
    def lineage(self, entity_id: str, depth: int = 10) -> dict[str, Any]:
        """Upstream provenance: how this entity came to be."""
        generated_by: dict[str, list[LedgerEntry]] = {}
        used_by_activity: dict[str, list[str]] = {}
        for e in self.ledger.query(kind="activity.generated"):
            generated_by.setdefault(e.data["entity_id"], []).append(e)
        for e in self.ledger.query(kind="activity.used"):
            used_by_activity.setdefault(e.data["activity_id"], []).append(e.data["entity_id"])
        acts = {a.activity_id: a for a in self.activities()}
        replaced = {e.data["installed"]: e.subject
                    for e in self.ledger.query(kind="entity.replaced")}

        def walk(eid: str, d: int, seen: frozenset[str]) -> dict[str, Any]:
            node: dict[str, Any] = {"entity": eid}
            if d <= 0 or eid in seen:
                return node
            seen = seen | {eid}
            gens = []
            for g in generated_by.get(eid, []):
                aid = g.data["activity_id"]
                a = acts.get(aid)
                gens.append({
                    "activity": aid, "kind": a.kind.value if a else None,
                    "title": a.title if a else None,
                    "status": a.status.value if a else None,
                    "used": [walk(u, d - 1, seen) for u in used_by_activity.get(aid, [])],
                    "derived_from": [walk(x, d - 1, seen)
                                     for x in g.data.get("derived_from", [])],
                })
            if gens:
                node["generated_by"] = gens
            if eid in replaced:
                node["replaces"] = walk(replaced[eid], d - 1, seen)
            return node

        return walk(entity_id, depth, frozenset())

    def impact(self, entity_id: str, depth: int = 10) -> dict[str, Any]:
        """Downstream provenance: everything that used or was derived from this entity."""
        users: dict[str, list[str]] = {}
        for e in self.ledger.query(kind="activity.used"):
            users.setdefault(e.data["entity_id"], []).append(e.data["activity_id"])
        outputs: dict[str, list[str]] = {}
        derived: dict[str, list[str]] = {}
        for e in self.ledger.query(kind="activity.generated"):
            outputs.setdefault(e.data["activity_id"], []).append(e.data["entity_id"])
            for src in e.data.get("derived_from", []):
                derived.setdefault(src, []).append(e.data["entity_id"])
        replaced_by = {e.subject: e.data["installed"]
                       for e in self.ledger.query(kind="entity.replaced")}

        def walk(eid: str, d: int, seen: frozenset[str]) -> dict[str, Any]:
            node: dict[str, Any] = {"entity": eid}
            if d <= 0 or eid in seen:
                return node
            seen = seen | {eid}
            used_in = [{"activity": aid,
                        "generated": [walk(o, d - 1, seen) for o in outputs.get(aid, [])]}
                       for aid in users.get(eid, [])]
            if used_in:
                node["used_in"] = used_in
            if derived.get(eid):
                node["derivatives"] = [walk(x, d - 1, seen) for x in derived[eid]]
            if eid in replaced_by:
                node["replaced_by"] = replaced_by[eid]
            return node

        return walk(entity_id, depth, frozenset())

    # -- runtime integration -----------------------------------------------------------
    def runtime_sink(self, kinds: Iterable[str] = DEFAULT_RUNTIME_KINDS
                     ) -> Callable[[Any], None]:
        """A :class:`~agentic_robotics.runtime.Trace` sink that copies significant runtime
        events into the project ledger, keeping their trace and span ids."""
        wanted = frozenset(kinds)

        def sink(event: Any) -> None:
            if event.kind in wanted:
                self.ledger.append(f"runtime.{event.kind}", event.subject,
                                   {"t": event.t, "clock_domain": event.clock_domain,
                                    **event.data},
                                   trace_id=event.trace_id, span_id=event.span_id)

        return sink


class ActivityRecorder(AbstractContextManager["ActivityRecorder"]):
    def __init__(self, project: Project, kind: ActivityKind, title: str,
                 requirements: tuple[str, ...], params: dict[str, Any],
                 actors: tuple[str, ...], parent: str | None) -> None:
        self.project = project
        self.kind = kind
        self.title = title
        self.requirements = requirements
        self.params = params
        self.actors = actors
        self.parent = parent
        self.activity_id = new_id("act")
        self.outcome = ""
        self.result: Activity | None = None
        self._used: list[str] = []
        self._generated: list[str] = []
        self._metrics: dict[str, Any] = {}
        self._notes: list[str] = []
        self._failure: str | None = None
        self._span: AbstractContextManager[tracing.SpanContext] | None = None
        self._ctx: tracing.SpanContext | None = None
        self._started = 0.0
        self._environment: dict[str, Any] = {}

    @property
    def trace_id(self) -> str | None:
        return self._ctx.trace_id if self._ctx else None

    def __enter__(self) -> ActivityRecorder:
        known = self.project.requirements
        if known is not None:
            unknown = [r for r in self.requirements if known.get(r) is None]
            if unknown:
                raise SchemaError("activity cites unknown requirements", unknown=unknown)
        self._span = tracing.span(f"activity:{self.kind.value}")
        self._ctx = self._span.__enter__()
        self._started = time.time()
        self._environment = capture_environment(self.project.root)
        fingerprints: dict[str, str] = {}
        if known is not None and self.requirements:
            from .traceability import requirement_fingerprints
            fingerprints = requirement_fingerprints(known, self.requirements)
        self.project.ledger.append("activity.started", self.activity_id, {
            "kind": self.kind.value, "title": self.title,
            "requirements": list(self.requirements), "params": self.params,
            "actors": list(self.actors), "parent_id": self.parent,
            "environment": self._environment,
            "requirement_fingerprints": fingerprints,
        })
        return self

    async def __aenter__(self) -> ActivityRecorder:
        return self.__enter__()

    async def __aexit__(self, et: type[BaseException] | None, ev: BaseException | None,
                        tb: TracebackType | None) -> None:
        self.__exit__(et, ev, tb)

    def use(self, source: str | Path | EntityRef, *, kind: str = "input",
            **entity_kw: Any) -> EntityRef:
        ref = source if isinstance(source, EntityRef) else (
            self._lookup(source) or self.project.entity(source, kind=kind, **entity_kw))
        self._used.append(ref.entity_id)
        self.project.ledger.append("activity.used", self.activity_id,
                                   {"activity_id": self.activity_id,
                                    "entity_id": ref.entity_id})
        return ref

    def generate(self, source: str | Path | bytes | EntityRef, *, kind: str = "output",
                 derived_from: Iterable[EntityRef | str] = (), store: bool = True,
                 **entity_kw: Any) -> EntityRef:
        ref = source if isinstance(source, EntityRef) else self.project.entity(
            source, kind=kind, store=store, **entity_kw)
        self._generated.append(ref.entity_id)
        self.project.ledger.append("activity.generated", self.activity_id, {
            "activity_id": self.activity_id, "entity_id": ref.entity_id,
            "derived_from": [d.entity_id if isinstance(d, EntityRef) else d
                             for d in derived_from],
        })
        return ref

    def note(self, text: str) -> None:
        self._notes.append(text)
        self.project.ledger.append("activity.note", self.activity_id, {"text": text})

    def metric(self, name: str, value: Any) -> None:
        self._metrics[name] = value

    def fail(self, reason: str) -> None:
        """Mark the activity failed without raising (e.g. an experiment was rejected)."""
        self._failure = reason

    def _lookup(self, source: str | Path) -> EntityRef | None:
        s = str(source)
        if ":" in s and "@" in s and not Path(s).exists():
            return self.project.entities().get(s)
        return None

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        error = repr(ev) if ev is not None else self._failure
        status = ActivityStatus.FAILED if error else ActivityStatus.SUCCEEDED
        self.result = Activity(
            activity_id=self.activity_id, kind=self.kind, title=self.title, status=status,
            started_at=self._started, ended_at=time.time(), actors=self.actors,
            requirements=self.requirements, used=tuple(self._used),
            generated=tuple(self._generated), parent_id=self.parent, trace_id=self.trace_id,
            params=self.params, environment=self._environment, metrics=self._metrics,
            notes=tuple(self._notes),
            outcome=self.outcome, error=error,
        )
        self.project.ledger.append("activity.finished", self.activity_id,
                                   {"activity": self.result.to_dict()})
        if self._span is not None:
            self._span.__exit__(et, ev, tb)
