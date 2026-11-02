"""Contracts for the package library: interfaces, packages, deployments, skill releases.

Patterns adopted (not code) from robonix (versioned capability contracts that callers
bind to instead of implementations; packages that provide them; a body that exports
them) and from rosclaw (skill packages that declare robot compatibility, provider
routes and lineage; staged promotion). See ADR 0020.

- An :class:`InterfaceContract` (``ar.interface/1.0``) is a typed API such as
  ``air/service/navigation/navigate``: inputs, outputs, failure kinds, a semver version
  and the *side effect* of calling it. ``physical`` interfaces can only be reached
  through runtime admission, never by a direct service call.
- A :class:`PackageManifest` (``ar.package/1.0``) says what a package provides
  (interfaces at a version), requires (interfaces and packages by version *range*),
  needs from the hardware (robot classes, device types with counts, protocols, frames)
  and from the host (os, architecture), and which skills it carries.
- A :class:`Deployment` (``ar.deployment/1.0``) assembles a robot from packages.
- A :class:`SkillRelease` (``ar.skill_release/1.0``) records one version of a skill: its
  stage, the package and digest it came from, the evidence behind its stage and who
  promoted it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..contracts.skill import ParamSpec, SkillSpec
from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from ..core.versions import valid_range, version_key

__requirements__ = ("REQ05", "REQ03", "REQ18", "G02")


def _check_id(value: str, what: str) -> None:
    parts = value.split("/")
    if len(parts) < 3 or not all(p and all(c.isalnum() or c in "_-." for c in p)
                                 for p in parts):
        raise SchemaError(f"invalid {what} id (namespace/kind/name...)", value=value)


def _check_version(value: str, what: str) -> None:
    if version_key(value)[0] == 0:
        raise SchemaError(f"{what} needs a numeric version", version=value)


class InterfaceKind(StrEnum):
    PRIMITIVE = "primitive"   # one hardware function behind a software interface
    SERVICE = "service"       # reusable software: planning, perception, mapping, ...
    SKILL = "skill"           # a goal-level capability, executed through the runtime


class InterfaceMode(StrEnum):
    RPC = "rpc"               # request -> response
    ACTION = "action"         # goal -> feedback -> result, cancellable
    STREAM = "stream"         # continuous data


class InterfaceEffect(StrEnum):
    NONE = "none"             # pure query
    COMPUTE = "compute"       # uses resources, changes nothing outside
    STATE = "state"           # changes software state (maps, memory, configuration)
    PHYSICAL = "physical"     # moves or acts in the world: only through admission


@contract("interface", version="1.0")
class InterfaceContract(Contract):
    interface_id: str                 # air/service/navigation/navigate
    version: str                      # semver; minor = additive, major = breaking
    kind: InterfaceKind
    description: str
    mode: InterfaceMode = InterfaceMode.RPC
    effect: InterfaceEffect = InterfaceEffect.NONE
    inputs: tuple[ParamSpec, ...] = ()
    outputs: tuple[ParamSpec, ...] = ()
    failure_kinds: tuple[str, ...] = ()
    #: Whether a planning model may call it directly (robonix ``llm_callable``).
    model_callable: bool = True
    deprecated_by: str | None = None

    def __post_init__(self) -> None:
        _check_id(self.interface_id, "interface")
        _check_version(self.version, "interface")
        if self.effect is InterfaceEffect.PHYSICAL and self.kind is InterfaceKind.SERVICE \
                and self.mode is InterfaceMode.STREAM:
            raise SchemaError("a physical interface cannot be a stream", id=self.interface_id)


@dataclass(frozen=True)
class Provide:
    interface: str
    version: str                      # the version of the interface this implements


@dataclass(frozen=True)
class Require:
    interface: str
    range: str = "*"                  # ^1.2 | ~1.2.3 | >=1.0,<2 | 1.4.0 | *
    optional: bool = False
    description: str = ""


@dataclass(frozen=True)
class PackageDependency:
    package: str
    range: str = "*"


@dataclass(frozen=True)
class DeviceRequirement:
    """A device the package needs: catalog type patterns and a minimum count."""

    types: tuple[str, ...]            # hardware catalog patterns, e.g. sensor.lidar.*
    min: int = 1
    description: str = ""


@dataclass(frozen=True)
class HardwareRequirements:
    """What the package needs from a robot. Empty means it needs nothing."""

    robot_classes: tuple[str, ...] = ()           # patterns; empty = any class
    devices: tuple[DeviceRequirement, ...] = ()
    protocols: tuple[str, ...] = ()               # protocol ids some component must speak
    frames: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()            # nominal capabilities of the robot
    incompatible_classes: tuple[str, ...] = ()    # known not to work, with reason in notes
    notes: str = ""


@dataclass(frozen=True)
class HostRequirements:
    os: tuple[str, ...] = ()                      # linux | windows | darwin; empty = any
    arch: tuple[str, ...] = ()                    # amd64 | arm64 | ...
    python: str = "*"                             # version range


class PackageKind(StrEnum):
    PRIMITIVE = "primitive"
    SERVICE = "service"
    SKILL = "skill"
    BUNDLE = "bundle"


@contract("package", version="1.0")
class PackageManifest(Contract):
    name: str                         # air/service/acme_nav (namespace/kind/name)
    version: str
    kind: PackageKind
    description: str
    license: str
    maintainers: tuple[str, ...] = ()
    provides: tuple[Provide, ...] = ()
    requires: tuple[Require, ...] = ()
    depends: tuple[PackageDependency, ...] = ()
    conflicts: tuple[PackageDependency, ...] = ()
    hardware: HardwareRequirements = field(default_factory=HardwareRequirements)
    host: HostRequirements = field(default_factory=HostRequirements)
    skills: tuple[SkillSpec, ...] = ()
    #: Configuration the package accepts (documented and checked at deployment).
    config: tuple[ParamSpec, ...] = ()
    #: How the package is started: an adapter kind, a service entry point, or nothing.
    entrypoint: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_id(self.name, "package")
        _check_version(self.version, "package")
        ranges = [r.range for r in self.requires] + [d.range for d in self.depends] + \
            [c.range for c in self.conflicts]
        for spec in ranges:
            if not valid_range(spec):
                raise SchemaError("invalid version range", package=self.name, range=spec)
        for p in self.provides:
            _check_version(p.version, f"provided {p.interface}")
        ids = [s.skill_id for s in self.skills]
        if len(ids) != len(set(ids)):
            raise SchemaError("duplicate skills in package", package=self.name)


@dataclass(frozen=True)
class DeployedPackage:
    package: str
    range: str = "*"
    config: dict[str, Any] = field(default_factory=dict)
    digest: str | None = None         # pin: the registry checks the published document


@contract("deployment", version="1.0")
class Deployment(Contract):
    """A robot assembled from packages."""

    deployment_id: str
    robot_id: str
    packages: tuple[DeployedPackage, ...]
    description: str = ""


class SkillStage(StrEnum):
    """Lifecycle of one skill version (rosclaw stages, made evidence-bound).

    ``validated_sim`` never authorizes hardware; ``validated_real`` needs hardware
    evidence; only ``validated_real`` may be selected for ``real`` runs.
    """

    DRAFT = "draft"
    CANDIDATE = "candidate"
    VALIDATED_SIM = "validated_sim"
    VALIDATED_REAL = "validated_real"
    DEPRECATED = "deprecated"
    REVOKED = "revoked"


class EvidenceDomain(StrEnum):
    REPLAY = "replay"
    SIMULATION = "simulation"
    HARDWARE = "hardware"


@dataclass(frozen=True)
class EvidenceRef:
    """A project activity that evaluated this skill version, and what it showed."""

    activity_id: str
    domain: EvidenceDomain
    passed: bool
    metrics: dict[str, float] = field(default_factory=dict)
    held_out: bool = False            # evaluated on seeds/scenarios not used to develop it


@contract("skill_release", version="1.0")
class SkillRelease(Contract):
    skill_id: str
    version: str
    package: str
    package_version: str
    package_digest: str
    stage: SkillStage = SkillStage.DRAFT
    produced_by: str = "unspecified"
    parent: str | None = None         # skill_id@version this one was derived from
    evidence: tuple[EvidenceRef, ...] = ()
    promoted_by: str | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        _check_version(self.version, "skill release")

    @property
    def ref(self) -> str:
        return f"{self.skill_id}@{self.version}"
