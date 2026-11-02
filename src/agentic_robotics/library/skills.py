"""Skill library: versioned skills with stages, evidence-bound promotion, champions,
rollback and deterministic, compatibility-checked resolution.

Adopted from rosclaw's Skill Hub (stages, lineage with package hash, re-evaluating at
promotion instead of trusting a stored report, simulation evidence never authorizing
hardware, rollback records, deterministic resolution that never auto-selects a poor
match). AIR changes three things:

- **A different principal promotes.** The producer of a skill version can never promote
  it, and agents (``agent:*``) can never promote to a validated stage: agents propose,
  people approve.
- **Evidence is project evidence.** Every evidence reference must name a finished
  activity in the project ledger; promotion re-checks the package against the digest
  recorded when it was released, and can re-run an evaluation (``reverify``) so an
  edited report cannot authorize anything.
- **Resolution checks the robot.** A version is selectable only if its package fits the
  robot (hardware, host) and its stage allows the run mode: ``real`` needs
  ``validated_real``.

State is event-sourced in the project ledger (``skill.*`` entries); packages live in the
content-addressed registry.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ..contracts.manifest import ComponentManifest
from ..contracts.skill import SkillSpec
from ..core.errors import AuthorityError, FrameworkError
from ..core.versions import version_key
from ..hardware.catalog import DeviceCatalog
from ..hardware.validate import Finding, Level
from ..provenance.project import Project
from ..registry import LocalRegistry
from ..skills import SkillRegistry
from .compat import HostFacts, check_package_on_robot, check_skill_on_robot
from .contracts import EvidenceDomain, EvidenceRef, PackageManifest, SkillRelease, SkillStage

__requirements__ = ("REQ05", "REQ11", "REQ16", "G03", "G15")

S = SkillStage

#: Allowed stage moves. Anything may be deprecated; revoked is terminal.
TRANSITIONS: dict[SkillStage, frozenset[SkillStage]] = {
    S.DRAFT: frozenset({S.CANDIDATE, S.DEPRECATED, S.REVOKED}),
    S.CANDIDATE: frozenset({S.VALIDATED_SIM, S.VALIDATED_REAL, S.DEPRECATED, S.REVOKED}),
    S.VALIDATED_SIM: frozenset({S.VALIDATED_REAL, S.DEPRECATED, S.REVOKED}),
    S.VALIDATED_REAL: frozenset({S.DEPRECATED, S.REVOKED}),
    S.DEPRECATED: frozenset({S.REVOKED}),
    S.REVOKED: frozenset(),
}
SELECTABLE = {"sim": (S.VALIDATED_SIM, S.VALIDATED_REAL), "real": (S.VALIDATED_REAL,),
              "dev": (S.CANDIDATE, S.VALIDATED_SIM, S.VALIDATED_REAL)}


class SkillLibraryError(FrameworkError):
    code = "skill_library_error"


def registry_name(package: str) -> str:
    """Registry asset name for a package (``air/service/nav`` -> ``pkg.air.service.nav``)."""
    return "pkg." + package.replace("/", ".")


def is_agent(principal: str) -> bool:
    return principal.startswith("agent:")


@dataclass(frozen=True)
class Resolution:
    skill_id: str
    mode: str
    release: SkillRelease | None
    spec: SkillSpec | None
    reasons: tuple[str, ...] = ()
    rejected: tuple[tuple[str, str], ...] = ()   # (ref, why)


@dataclass
class _State:
    releases: dict[str, SkillRelease] = field(default_factory=dict)   # ref -> release
    champions: dict[tuple[str, str], str] = field(default_factory=dict)  # (skill, mode) -> ref
    history: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class SkillLibrary:
    def __init__(self, project: Project, registry: LocalRegistry,
                 catalog: DeviceCatalog | None = None) -> None:
        self.project = project
        self.registry = registry
        self.catalog = catalog

    # -- state --------------------------------------------------------------------------
    def _state(self) -> _State:
        st = _State()
        for e in self.project.ledger.query(prefix="skill."):
            ref = e.subject
            st.history.setdefault(ref.split("#", 1)[0], []).append(
                {"seq": e.seq, "kind": e.kind, **e.data})
            if e.kind == "skill.released":
                st.releases[ref] = SkillRelease.from_dict(e.data["release"])
            elif e.kind == "skill.stage_changed" and ref in st.releases:
                r = st.releases[ref]
                evidence = tuple(EvidenceRef(**{**x, "domain": EvidenceDomain(x["domain"])})
                                 for x in e.data.get("evidence", []))
                st.releases[ref] = dataclasses.replace(
                    r, stage=SkillStage(e.data["to"]), promoted_by=e.data["by"],
                    evidence=r.evidence + evidence, reason=e.data.get("reason", ""))
            elif e.kind == "skill.champion":
                st.champions[(e.data["skill_id"], e.data["mode"])] = e.data["ref"]
        return st

    def releases(self, skill_id: str | None = None) -> list[SkillRelease]:
        out = [r for r in self._state().releases.values()
               if skill_id is None or r.skill_id == skill_id]
        return sorted(out, key=lambda r: (r.skill_id, version_key(r.version)))

    def get(self, ref: str) -> SkillRelease:
        found = self._state().releases.get(ref)
        if found is None:
            raise SkillLibraryError("unknown skill release", ref=ref)
        return found

    def champion(self, skill_id: str, mode: str) -> str | None:
        return self._state().champions.get((skill_id, mode))

    def lineage(self, skill_id: str) -> list[dict[str, Any]]:
        st = self._state()
        out = []
        for ref, events in st.history.items():
            if ref.split("@", 1)[0] == skill_id or ref == skill_id:
                out.extend(events)
        return sorted(out, key=lambda x: x["seq"])

    # -- publishing ---------------------------------------------------------------------
    def publish(self, pkg: PackageManifest, *, by: str,
                parents: dict[str, str] | None = None) -> list[SkillRelease]:
        """Publish a package (immutable) and release each skill it carries as a draft."""
        ref = self.registry.publish(registry_name(pkg.name), pkg.version, pkg)
        out = []
        known = self._state().releases
        for skill in pkg.skills:
            rel = SkillRelease(skill_id=skill.skill_id, version=skill.version,
                               package=pkg.name, package_version=pkg.version,
                               package_digest=ref.digest, produced_by=by,
                               parent=(parents or {}).get(skill.skill_id))
            if rel.ref in known:
                if known[rel.ref].package_digest != ref.digest:
                    raise SkillLibraryError("a skill version is immutable; bump its version",
                                            ref=rel.ref)
                continue
            self.project.ledger.append("skill.released", rel.ref,
                                       {"release": rel.to_dict(), "by": by})
            out.append(rel)
        return out

    def package_of(self, release: SkillRelease) -> PackageManifest:
        """The exact package the release came from (digest re-checked)."""
        doc = self.registry.fetch(registry_name(release.package), release.package_version,
                                  digest=release.package_digest)
        assert isinstance(doc, PackageManifest)
        return doc

    def spec_of(self, release: SkillRelease) -> SkillSpec:
        pkg = self.package_of(release)
        return next(s for s in pkg.skills if s.skill_id == release.skill_id)

    # -- promotion ----------------------------------------------------------------------
    def promote(self, ref: str, to: SkillStage | str, *, by: str,
                evidence: Iterable[EvidenceRef] = (), reason: str = "",
                reverify: Callable[[SkillRelease], EvidenceRef] | None = None
                ) -> SkillRelease:
        to = SkillStage(to)
        rel = self.get(ref)
        if to not in TRANSITIONS[rel.stage]:
            raise SkillLibraryError("stage change not allowed", ref=ref, frm=rel.stage.value,
                                    to=to.value)
        validating = to in (S.VALIDATED_SIM, S.VALIDATED_REAL)
        if validating and (by == rel.produced_by or is_agent(by)):
            raise AuthorityError("a skill is validated by a person other than its producer; "
                                 "agents never validate", principal=by, producer=rel.produced_by)
        if to is S.REVOKED and not reason:
            raise SkillLibraryError("revoking needs a reason", ref=ref)
        self.package_of(rel)                      # the package is still what was released
        new = list(evidence)
        if reverify is not None:
            fresh = reverify(rel)                 # a stored report is a cache, not a verdict
            new.append(fresh)
            if not fresh.passed:
                raise SkillLibraryError("re-verification failed; promotion blocked", ref=ref)
        if validating:
            self._check_evidence(rel, to, [*rel.evidence, *new])
        self.project.ledger.append("skill.stage_changed", ref, {
            "frm": rel.stage.value, "to": to.value, "by": by, "reason": reason,
            "evidence": [{**dataclasses.asdict(x), "domain": x.domain.value} for x in new]})
        if to in (S.DEPRECATED, S.REVOKED):
            for mode in SELECTABLE:
                if self.champion(rel.skill_id, mode) == ref:
                    self.project.ledger.append("skill.champion", f"{rel.skill_id}#{mode}", {
                        "skill_id": rel.skill_id, "mode": mode, "ref": None, "by": by,
                        "reason": f"{ref} {to.value}"})
        return self.get(ref)

    def _check_evidence(self, rel: SkillRelease, to: SkillStage,
                        evidence: list[EvidenceRef]) -> None:
        finished = {a.activity_id: a for a in self.project.activities()}
        for x in evidence:
            act = finished.get(x.activity_id)
            if act is None or act.status.value != "succeeded":
                raise SkillLibraryError("evidence must be a succeeded project activity",
                                        ref=rel.ref, activity=x.activity_id)
        passing = [x for x in evidence if x.passed and x.held_out]
        if to is S.VALIDATED_SIM and not any(
                x.domain in (EvidenceDomain.SIMULATION, EvidenceDomain.HARDWARE)
                for x in passing):
            raise SkillLibraryError("validated_sim needs passing held-out simulation or "
                                    "hardware evidence", ref=rel.ref)
        if to is S.VALIDATED_REAL and not any(x.domain is EvidenceDomain.HARDWARE
                                              for x in passing):
            raise SkillLibraryError("validated_real needs passing held-out hardware evidence; "
                                    "simulation never authorizes hardware", ref=rel.ref)
        if any(not x.passed for x in evidence if x.domain is EvidenceDomain.HARDWARE):
            raise SkillLibraryError("a failed hardware evaluation blocks validation until a "
                                    "newer version passes", ref=rel.ref)

    def set_champion(self, ref: str, mode: str, *, by: str, reason: str = "") -> None:
        rel = self.get(ref)
        if rel.stage not in SELECTABLE[mode]:
            raise SkillLibraryError("stage not selectable for this mode", ref=ref, mode=mode,
                                    stage=rel.stage.value)
        if is_agent(by):
            raise AuthorityError("agents do not choose champions", principal=by)
        previous = self.champion(rel.skill_id, mode)
        self.project.ledger.append("skill.champion", f"{rel.skill_id}#{mode}", {
            "skill_id": rel.skill_id, "mode": mode, "ref": ref, "previous": previous,
            "by": by, "reason": reason})

    def rollback(self, skill_id: str, mode: str, to_ref: str, *, by: str,
                 reason: str) -> None:
        if not reason:
            raise SkillLibraryError("a rollback needs a reason", skill_id=skill_id)
        if self.get(to_ref).skill_id != skill_id:
            raise SkillLibraryError("rollback target is a different skill", ref=to_ref)
        self.set_champion(to_ref, mode, by=by, reason=f"rollback: {reason}")

    # -- resolution ---------------------------------------------------------------------
    def resolve(self, skill_id: str, robot: ComponentManifest, mode: str = "real",
                host: HostFacts | None = None) -> Resolution:
        """Deterministic: the champion if it fits, else the newest fitting version whose
        stage the mode allows. Never a best guess."""
        if mode not in SELECTABLE:
            raise SkillLibraryError("unknown mode", mode=mode, modes=sorted(SELECTABLE))
        st = self._state()
        versions = sorted((r for r in st.releases.values() if r.skill_id == skill_id),
                          key=lambda r: version_key(r.version), reverse=True)
        champ = st.champions.get((skill_id, mode))
        if champ:
            versions.sort(key=lambda r: r.ref != champ)
        rejected: list[tuple[str, str]] = []
        for rel in versions:
            if rel.stage not in SELECTABLE[mode]:
                rejected.append((rel.ref, f"stage {rel.stage.value} not allowed for {mode}"))
                continue
            try:
                pkg = self.package_of(rel)
            except Exception as exc:  # integrity or missing: never selectable
                rejected.append((rel.ref, f"package unavailable: {exc}"))
                continue
            spec = next(s for s in pkg.skills if s.skill_id == skill_id)
            problems = [f for f in check_package_on_robot(pkg, robot, self.catalog, host)
                        + check_skill_on_robot(spec, robot) if f.level is Level.ERROR]
            if problems:
                rejected.append((rel.ref, "; ".join(f.message for f in problems)))
                continue
            reasons = ["champion" if rel.ref == champ else "newest compatible",
                       f"stage {rel.stage.value}", f"package {pkg.name}@{pkg.version}"]
            return Resolution(skill_id, mode, rel, spec, tuple(reasons), tuple(rejected))
        return Resolution(skill_id, mode, None, None, ("nothing selectable",), tuple(rejected))

    def registry_for(self, robot: ComponentManifest, skill_ids: Iterable[str],
                     mode: str = "real", host: HostFacts | None = None
                     ) -> tuple[SkillRegistry, list[Finding]]:
        """A runtime SkillRegistry of the versions resolved for this robot and mode."""
        specs, findings = [], []
        for sid in skill_ids:
            res = self.resolve(sid, robot, mode, host)
            if res.spec is None:
                why = "; ".join(f"{r}: {w}" for r, w in res.rejected) or "no releases"
                findings.append(Finding(Level.ERROR, sid, f"no {mode} version fits "
                                                          f"{robot.robot_id} ({why})"))
            else:
                specs.append(res.spec)
        return SkillRegistry(specs), findings
