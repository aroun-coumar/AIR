"""Compatibility checks: package ↔ robot, package ↔ installed set, skill ↔ robot.

robonix binds callers to shared contract ids but records no version ranges or robot
requirements in its catalog; rosclaw checks host OS and architecture when resolving a
skill. AIR checks all of it before anything is installed or selected:

- **package ↔ robot**: the package's own hardware needs (robot classes, device types with
  counts, protocols, frames, nominal capabilities, known-incompatible classes) against a
  robot manifest and the hardware catalog, and its host needs against this host.
- **installed set**: every requirement is provided by some package at a version inside
  its range, every dependency is present at an acceptable version, no conflicts, no
  dependency cycles, and every provided interface version exists in the interface
  catalog.
- **skill ↔ robot**: the capabilities a skill requires are ones the robot has.

These are the package's requirements, not AIR's: a package that needs a lidar cannot
run on a robot without one. AIR itself still requires no hardware.
"""

from __future__ import annotations

import platform
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..contracts.capability import CapabilityReport
from ..contracts.manifest import ComponentManifest
from ..contracts.skill import SkillSpec
from ..core.versions import satisfies
from ..hardware.catalog import DeviceCatalog, matches
from ..hardware.validate import Finding, Level
from .contracts import PackageManifest
from .interfaces import InterfaceCatalog

__requirements__ = ("REQ05", "REQ03", "G02")


@dataclass(frozen=True)
class HostFacts:
    os: str
    arch: str
    python: str

    @classmethod
    def detect(cls) -> HostFacts:
        machine = platform.machine().lower()
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
                "arm64": "arm64"}.get(machine, machine)
        system = {"win32": "windows", "darwin": "darwin"}.get(sys.platform,
                                                              sys.platform.split("-")[0])
        return cls(os=system, arch=arch, python=platform.python_version())


def check_package_on_robot(pkg: PackageManifest, robot: ComponentManifest,
                           catalog: DeviceCatalog | None = None,
                           host: HostFacts | None = None) -> list[Finding]:
    where = f"{pkg.name}@{pkg.version}"
    hw = pkg.hardware
    out: list[Finding] = []
    rc = robot.robot_class
    for pattern in hw.incompatible_classes:
        if rc and matches(rc, pattern):
            out.append(Finding(Level.ERROR, where, f"declared incompatible with {rc}"
                                                   + (f": {hw.notes}" if hw.notes else "")))
    if hw.robot_classes:
        if rc is None:
            out.append(Finding(Level.WARNING, where, "robot declares no robot_class; cannot "
                                                     f"check {list(hw.robot_classes)}"))
        elif not any(matches(rc, p) for p in hw.robot_classes):
            out.append(Finding(Level.ERROR, where, f"needs a robot class in "
                                                   f"{list(hw.robot_classes)}, robot is {rc}"))
    typed = [c for c in robot.components if c.type_id]
    for need in hw.devices:
        n = sum(any(matches(c.type_id or "", p) for p in need.types) for c in typed)
        if n < need.min:
            unknown = catalog is not None and not any(
                catalog.match(p) or catalog.match_classes(p) for p in need.types)
            note = " (no catalog type matches these patterns)" if unknown else ""
            out.append(Finding(Level.ERROR, where,
                               f"needs at least {need.min} of {list(need.types)}, robot has "
                               f"{n}{note}" + (f": {need.description}" if need.description
                                               else "")))
    spoken = {i.kind for c in robot.components for i in c.interfaces}
    for proto in hw.protocols:
        if proto not in spoken:
            out.append(Finding(Level.ERROR, where, f"needs a component speaking {proto}"))
    for frame in hw.frames:
        if frame not in robot.frames:
            out.append(Finding(Level.ERROR, where, f"needs frame {frame!r}"))
    for cap in hw.capabilities:
        if cap not in robot.nominal_capabilities:
            out.append(Finding(Level.ERROR, where, f"needs robot capability {cap!r}"))
    if host is not None:
        if pkg.host.os and host.os not in pkg.host.os:
            out.append(Finding(Level.ERROR, where, f"needs os in {list(pkg.host.os)}, host "
                                                   f"is {host.os}"))
        if pkg.host.arch and host.arch not in pkg.host.arch:
            out.append(Finding(Level.ERROR, where, f"needs arch in {list(pkg.host.arch)}, "
                                                   f"host is {host.arch}"))
        if not satisfies(host.python, pkg.host.python):
            out.append(Finding(Level.ERROR, where, f"needs python {pkg.host.python}, host "
                                                   f"has {host.python}"))
    return out


def providers(packages: Iterable[PackageManifest]) -> dict[str, list[tuple[str, str]]]:
    """interface id -> [(package name, provided version)]."""
    out: dict[str, list[tuple[str, str]]] = {}
    for pkg in packages:
        for p in pkg.provides:
            out.setdefault(p.interface, []).append((pkg.name, p.version))
    return out


def check_installation(packages: Iterable[PackageManifest],
                       interfaces: InterfaceCatalog | None = None) -> list[Finding]:
    pkgs = list(packages)
    by_name: dict[str, PackageManifest] = {}
    out: list[Finding] = []
    for p in pkgs:
        if p.name in by_name:
            out.append(Finding(Level.ERROR, p.name, "installed twice "
                                                    f"({by_name[p.name].version}, {p.version})"))
        by_name[p.name] = p
    offered = providers(pkgs)
    for p in pkgs:
        where = f"{p.name}@{p.version}"
        for r in p.requires:
            ok = [(n, v) for n, v in offered.get(r.interface, []) if satisfies(v, r.range)]
            if not ok:
                have = offered.get(r.interface)
                detail = (f"; provided only at {[v for _, v in have]}" if have
                          else "; no package provides it")
                out.append(Finding(Level.WARNING if r.optional else Level.ERROR, where,
                                   f"requires {r.interface} {r.range}{detail}"))
            if interfaces is not None and interfaces.best(r.interface, r.range) is None:
                out.append(Finding(Level.WARNING, where,
                                   f"no interface contract {r.interface} {r.range} in the "
                                   f"catalog"))
        for d in p.depends:
            dep = by_name.get(d.package)
            if dep is None:
                out.append(Finding(Level.ERROR, where, f"depends on {d.package} {d.range}, "
                                                       f"not installed"))
            elif not satisfies(dep.version, d.range):
                out.append(Finding(Level.ERROR, where, f"depends on {d.package} {d.range}, "
                                                       f"installed {dep.version}"))
        for c in p.conflicts:
            other = by_name.get(c.package)
            if other is not None and satisfies(other.version, c.range):
                out.append(Finding(Level.ERROR, where, f"conflicts with installed "
                                                       f"{other.name}@{other.version}"))
        if interfaces is not None:
            out.extend(check_provided(p, interfaces))
    out.extend(_cycles(by_name))
    return out


def check_provided(pkg: PackageManifest, interfaces: InterfaceCatalog) -> list[Finding]:
    """A package may only claim interface versions the catalog defines."""
    out = []
    for prov in pkg.provides:
        if interfaces.get(prov.interface, prov.version) is None:
            known = interfaces.versions(prov.interface)
            out.append(Finding(Level.ERROR if known else Level.WARNING,
                               f"{pkg.name}@{pkg.version}",
                               f"provides {prov.interface} {prov.version}, which is "
                               + (f"not a catalog version ({known})" if known
                                  else "not in the interface catalog")))
    return out


def _cycles(by_name: Mapping[str, PackageManifest]) -> list[Finding]:
    out: list[Finding] = []
    state: dict[str, int] = {}

    def visit(name: str, path: list[str]) -> None:
        state[name] = 1
        for d in by_name[name].depends:
            if d.package not in by_name:
                continue
            if state.get(d.package) == 1:
                cycle = [*path[path.index(d.package):], d.package] if d.package in path \
                    else [name, d.package]
                out.append(Finding(Level.ERROR, name, "dependency cycle: " + " -> ".join(cycle)))
            elif d.package not in state:
                visit(d.package, [*path, d.package])
        state[name] = 2

    for name in sorted(by_name):
        if name not in state:
            visit(name, [name])
    return out


def check_skill_on_robot(skill: SkillSpec, robot: ComponentManifest,
                         report: CapabilityReport | None = None) -> list[Finding]:
    """Static check of a skill's required capabilities; admission re-checks at run time."""
    have = set(robot.nominal_capabilities)
    if report is not None:
        have |= {p.name for p in report.profiles}
    return [Finding(Level.ERROR, skill.skill_id, f"needs capability {cap!r}, robot "
                                                 f"{robot.robot_id} does not have it")
            for cap in skill.required_capabilities if cap not in have]
