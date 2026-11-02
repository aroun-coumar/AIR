"""``air pkg``, ``air interfaces`` and ``air skills``: packages, contracts, skill library."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..contracts.manifest import ComponentManifest
from ..core.schema import _dump
from ..hardware.catalog import project_catalog
from ..hardware.validate import Finding, Level
from ..registry import LocalRegistry
from .compat import HostFacts, check_installation, check_package_on_robot, check_provided
from .contracts import Deployment, EvidenceDomain, EvidenceRef, PackageManifest, SkillStage
from .interfaces import project_interfaces
from .skills import SkillLibrary, registry_name

__requirements__ = ("REQ05", "REQ18")


def _print(findings: list[Finding], title: str) -> int:
    errors = [f for f in findings if f.level is Level.ERROR]
    print(f"{title}: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
    for f in findings:
        print(f"  {f}")
    return 1 if errors else 0


def _registry(args: argparse.Namespace) -> LocalRegistry:
    return LocalRegistry(Path(args.project) / args.registry)


def _library(args: argparse.Namespace) -> SkillLibrary:
    from ..provenance.project import Project

    return SkillLibrary(Project.open(args.project), _registry(args),
                        project_catalog(args.project))


def cmd_pkg_check(args: argparse.Namespace) -> int:
    pkgs = [PackageManifest.load(p) for p in args.packages]
    findings = check_installation(pkgs, project_interfaces(args.project))
    if args.robot:
        robot = ComponentManifest.load(args.robot)
        host = None if args.no_host else HostFacts.detect()
        for pkg in pkgs:
            findings += check_package_on_robot(pkg, robot, project_catalog(args.project), host)
    return _print(findings, ", ".join(f"{p.name}@{p.version}" for p in pkgs))


def cmd_pkg_publish(args: argparse.Namespace) -> int:
    pkg = PackageManifest.load(args.package)
    # Requirements are met at deployment; publishing only checks what the package claims.
    problems = [f for f in check_provided(pkg, project_interfaces(args.project))
                if f.level is Level.ERROR]
    if problems:
        return _print(problems, f"{pkg.name}@{pkg.version} not published")
    released = _library(args).publish(pkg, by=args.by)
    print(f"published {registry_name(pkg.name)}@{pkg.version}; released "
          f"{', '.join(r.ref for r in released) or 'no new skills'} as draft")
    return 0


def cmd_pkg_list(args: argparse.Namespace) -> int:
    for ref in _registry(args).list():
        if ref.name.startswith("pkg."):
            print(f"{ref.name}@{ref.version}  {ref.digest[:19]}")
    return 0


def cmd_deploy_check(args: argparse.Namespace) -> int:
    dep = Deployment.load(args.deployment)
    robot = ComponentManifest.load(args.robot)
    reg = _registry(args)
    pkgs, findings = [], []
    from ..core.versions import satisfies, version_key
    for d in dep.packages:
        name = registry_name(d.package)
        versions = [v for v in reg.versions(name) if satisfies(v, d.range)]
        if not versions:
            findings.append(Finding(Level.ERROR, d.package, f"no published version in {d.range}"))
            continue
        version = max(versions, key=version_key)
        doc = reg.fetch(name, version, digest=d.digest)
        assert isinstance(doc, PackageManifest)
        pkgs.append(doc)
        names = {c.name for c in doc.config}
        findings += [Finding(Level.ERROR, d.package, f"unknown config key {k}")
                     for k in d.config if k not in names]
        findings += [Finding(Level.ERROR, d.package, problem) for c in doc.config
                     if c.name in d.config and (problem := c.check(d.config[c.name]))]
        findings += [Finding(Level.ERROR, d.package, f"missing required config {c.name}")
                     for c in doc.config if c.required and c.name not in d.config]
    findings += check_installation(pkgs, project_interfaces(args.project))
    host = None if args.no_host else HostFacts.detect()
    for pkg in pkgs:
        findings += check_package_on_robot(pkg, robot, project_catalog(args.project), host)
    return _print(findings, f"deployment {dep.deployment_id} on {robot.robot_id}")


def cmd_interfaces_list(args: argparse.Namespace) -> int:
    cat = project_interfaces(args.project)
    for iid in cat.ids():
        c = cat.get(iid)
        assert c is not None
        print(f"{iid:<44} {','.join(cat.versions(iid)):<14} {c.kind.value:<9} "
              f"{c.effect.value:<8} {c.mode.value}")
    return 0


def cmd_interfaces_show(args: argparse.Namespace) -> int:
    c = project_interfaces(args.project).get(args.interface, args.version)
    if c is None:
        print(f"unknown interface {args.interface} {args.version or ''}", file=sys.stderr)
        return 2
    print(json.dumps(_dump(c), indent=2))
    return 0


def cmd_skills_releases(args: argparse.Namespace) -> int:
    lib = _library(args)
    for r in lib.releases(args.skill):
        champs = [m for m in ("dev", "sim", "real") if lib.champion(r.skill_id, m) == r.ref]
        flag = f"  champion: {','.join(champs)}" if champs else ""
        print(f"{r.ref:<36} {r.stage.value:<15} {r.package}@{r.package_version}  "
              f"by {r.produced_by}{flag}")
    return 0


def _evidence(items: list[str] | None) -> list[EvidenceRef]:
    out = []
    for raw in items or []:
        parts = raw.split(":")
        if len(parts) < 3:
            raise SystemExit("--evidence ACTIVITY:DOMAIN:pass|fail[:held_out]")
        act_id, domain, verdict = parts[0], parts[1], parts[2]
        out.append(EvidenceRef(activity_id=act_id, domain=EvidenceDomain(domain),
                               passed=verdict == "pass", held_out="held_out" in parts[3:]))
    return out


def cmd_skills_promote(args: argparse.Namespace) -> int:
    rel = _library(args).promote(args.ref, SkillStage(args.stage), by=args.by,
                                 evidence=_evidence(args.evidence), reason=args.reason or "")
    print(f"{rel.ref}: {rel.stage.value} (by {args.by})")
    return 0


def cmd_skills_champion(args: argparse.Namespace) -> int:
    _library(args).set_champion(args.ref, args.mode, by=args.by, reason=args.reason or "")
    print(f"{args.ref} is the {args.mode} champion")
    return 0


def cmd_skills_rollback(args: argparse.Namespace) -> int:
    _library(args).rollback(args.skill, args.mode, args.to, by=args.by, reason=args.reason)
    print(f"{args.skill} {args.mode} champion rolled back to {args.to}")
    return 0


def cmd_skills_resolve(args: argparse.Namespace) -> int:
    robot = ComponentManifest.load(args.robot)
    res = _library(args).resolve(args.skill, robot, args.mode,
                                 None if args.no_host else HostFacts.detect())
    out: dict[str, Any] = {"skill_id": res.skill_id, "mode": res.mode,
                           "selected": res.release.ref if res.release else None,
                           "reasons": list(res.reasons),
                           "rejected": [{"ref": r, "why": w} for r, w in res.rejected]}
    print(json.dumps(out, indent=2))
    return 0 if res.release else 1


def cmd_skills_lineage(args: argparse.Namespace) -> int:
    for e in _library(args).lineage(args.skill):
        detail = {k: v for k, v in e.items() if k not in ("seq", "kind", "release")}
        print(f"#{e['seq']:<5} {e['kind']:<20} {json.dumps(detail, sort_keys=True)[:140]}")
    return 0


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    def proj(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--project", default=".")
        p.add_argument("--registry", default=".ar-registry",
                       help="registry directory, relative to the project")
        return p

    pk = sub.add_parser("pkg", help="packages: provides/requires, compatibility, publishing")
    ps = pk.add_subparsers(dest="action", required=True)
    c = proj(ps.add_parser("check", help="check packages together, optionally on a robot"))
    c.add_argument("packages", nargs="+")
    c.add_argument("--robot", help="robot ComponentManifest")
    c.add_argument("--no-host", action="store_true", help="skip os/arch/python checks")
    c.set_defaults(fn=cmd_pkg_check)
    p = proj(ps.add_parser("publish", help="publish a package; its skills become drafts"))
    p.add_argument("package")
    p.add_argument("--by", required=True)
    p.set_defaults(fn=cmd_pkg_publish)
    proj(ps.add_parser("list", help="published packages")).set_defaults(fn=cmd_pkg_list)
    d = proj(ps.add_parser("deploy-check", help="resolve and check a deployment on a robot"))
    d.add_argument("deployment")
    d.add_argument("--robot", required=True)
    d.add_argument("--no-host", action="store_true")
    d.set_defaults(fn=cmd_deploy_check)

    it = sub.add_parser("interfaces", help="interface contracts packages provide and require")
    its = it.add_subparsers(dest="action", required=True)
    proj(its.add_parser("list")).set_defaults(fn=cmd_interfaces_list)
    sh = proj(its.add_parser("show"))
    sh.add_argument("interface")
    sh.add_argument("--version")
    sh.set_defaults(fn=cmd_interfaces_show)

    sk = sub.add_parser("skills", help="skill library: releases, promotion, champions, "
                                       "rollback, resolution")
    ss = sk.add_subparsers(dest="action", required=True)
    r = proj(ss.add_parser("releases"))
    r.add_argument("skill", nargs="?")
    r.set_defaults(fn=cmd_skills_releases)
    pr = proj(ss.add_parser("promote", help="move a release to a stage (evidence-bound)"))
    pr.add_argument("ref", help="skill_id@version")
    pr.add_argument("stage", choices=[s.value for s in SkillStage])
    pr.add_argument("--by", required=True)
    pr.add_argument("--evidence", action="append",
                    help="ACTIVITY_ID:replay|simulation|hardware:pass|fail[:held_out]")
    pr.add_argument("--reason")
    pr.set_defaults(fn=cmd_skills_promote)
    ch = proj(ss.add_parser("champion", help="select the version used for a mode"))
    ch.add_argument("ref")
    ch.add_argument("--mode", choices=["dev", "sim", "real"], required=True)
    ch.add_argument("--by", required=True)
    ch.add_argument("--reason")
    ch.set_defaults(fn=cmd_skills_champion)
    rb = proj(ss.add_parser("rollback"))
    rb.add_argument("skill")
    rb.add_argument("--mode", choices=["dev", "sim", "real"], required=True)
    rb.add_argument("--to", required=True, help="skill_id@version")
    rb.add_argument("--by", required=True)
    rb.add_argument("--reason", required=True)
    rb.set_defaults(fn=cmd_skills_rollback)
    rs = proj(ss.add_parser("resolve", help="which version a robot would run, and why"))
    rs.add_argument("skill")
    rs.add_argument("--robot", required=True)
    rs.add_argument("--mode", choices=["dev", "sim", "real"], default="real")
    rs.add_argument("--no-host", action="store_true")
    rs.set_defaults(fn=cmd_skills_resolve)
    ln = proj(ss.add_parser("lineage"))
    ln.add_argument("skill")
    ln.set_defaults(fn=cmd_skills_lineage)
