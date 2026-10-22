"""Robot and component manifests (baseline section 6, ComponentManifest)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.ids import is_valid_id
from ..core.schema import Contract, contract

__requirements__ = ("REQ01", "REQ03", "REQ16")


@dataclass(frozen=True)
class InterfaceSpec:
    name: str
    kind: str
    description: str = ""


class Sensitivity(StrEnum):
    """Who may see a fact. ``restricted`` (e.g. vendor NDA data) never leaves the device."""

    PUBLIC = "public"
    INTERNAL = "internal"
    RESTRICTED = "restricted"


class RelationKind(StrEnum):
    """Typed edges between components, beyond the mounting tree given by ``parent``."""

    MOUNTED_ON = "mounted_on"
    POWERED_BY = "powered_by"
    DRIVES = "drives"
    SENSES = "senses"
    COMMUNICATES_VIA = "communicates_via"
    PROTECTS = "protects"
    COOLS = "cools"
    CONTROLS = "controls"


#: How a relation reads from its target's side.
INVERSE_RELATION = {
    RelationKind.MOUNTED_ON: "carries", RelationKind.POWERED_BY: "powers",
    RelationKind.DRIVES: "driven_by", RelationKind.SENSES: "sensed_by",
    RelationKind.COMMUNICATES_VIA: "carries_traffic_of", RelationKind.PROTECTS: "protected_by",
    RelationKind.COOLS: "cooled_by", RelationKind.CONTROLS: "controlled_by",
}


@dataclass(frozen=True)
class Relation:
    kind: RelationKind
    target: str
    description: str = ""


@dataclass(frozen=True)
class LimitOverride:
    """This unit's limits for a catalog telemetry signal (from its datasheet or a test)."""

    signal: str
    warn_above: float | None = None
    critical_above: float | None = None
    warn_below: float | None = None
    critical_below: float | None = None
    reason: str = ""                     # e.g. "datasheet rev C, table 4"


@dataclass(frozen=True)
class DocumentRef:
    """A datasheet, drawing, test report or manual for a component, pinned by digest."""

    path: str                            # project-relative
    digest: str                          # sha256:<hex>
    kind: str = "datasheet"
    title: str = ""
    sensitivity: Sensitivity = Sensitivity.INTERNAL


@dataclass(frozen=True)
class ComponentSpec:
    component_id: str
    kind: str
    revision: str
    parent: str | None = None
    interfaces: tuple[InterfaceSpec, ...] = ()
    serial: str | None = None
    #: Hardware catalog type (``actuator.motor.bldc``); see ``agentic_robotics.hardware``.
    type_id: str | None = None
    vendor: str | None = None
    model: str | None = None
    #: Datasheet values by catalog property name: a number in the property's unit,
    #: ``{"value": 48, "unit": "V"}``, a string, a bool or an enum value.
    properties: dict[str, Any] = field(default_factory=dict)
    relations: tuple[Relation, ...] = ()
    limits: tuple[LimitOverride, ...] = ()
    documents: tuple[DocumentRef, ...] = ()
    #: Sensitivity of this component's details (properties, limits, documents).
    sensitivity: Sensitivity = Sensitivity.INTERNAL


@dataclass(frozen=True)
class AdapterRef:
    """Which adapter implements this robot, and its adapter-specific configuration."""

    kind: str
    profile: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


@contract("component_manifest", version="1.1")
class ComponentManifest(Contract):
    robot_id: str
    name: str
    hardware_revision: str
    adapter: AdapterRef
    components: tuple[ComponentSpec, ...] = ()
    nominal_capabilities: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()
    description: str = ""
    #: Hardware catalog robot class (``robot.legged.quadruped``), checked by
    #: ``air hardware check``.
    robot_class: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_id(self.robot_id):
            raise SchemaError("invalid robot_id", robot_id=self.robot_id)
        ids = [c.component_id for c in self.components]
        if len(ids) != len(set(ids)):
            raise SchemaError("duplicate component ids", robot_id=self.robot_id)
        known = set(ids)
        for c in self.components:
            if c.parent is not None and c.parent not in known:
                raise SchemaError("component parent not found", component=c.component_id,
                                  parent=c.parent)
            for r in c.relations:
                if r.target not in known or r.target == c.component_id:
                    raise SchemaError("relation target not found", component=c.component_id,
                                      relation=r.kind.value, target=r.target)

    def component(self, component_id: str) -> ComponentSpec | None:
        return next((c for c in self.components if c.component_id == component_id), None)

    def neighbours(self, component_id: str) -> list[tuple[str, str]]:
        """(relation, other component) pairs, both directions, including parent/child."""
        out: list[tuple[str, str]] = []
        for c in self.components:
            if c.component_id == component_id:
                if c.parent:
                    out.append(("part_of", c.parent))
                out.extend((r.kind.value, r.target) for r in c.relations)
            else:
                if c.parent == component_id:
                    out.append(("has_part", c.component_id))
                out.extend((INVERSE_RELATION[r.kind], c.component_id)
                           for r in c.relations if r.target == component_id)
        return out
