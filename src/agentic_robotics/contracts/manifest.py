"""Robot and component manifests (baseline section 6, ComponentManifest)."""

from __future__ import annotations

from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class ComponentSpec:
    component_id: str
    kind: str
    revision: str
    parent: str | None = None
    interfaces: tuple[InterfaceSpec, ...] = ()
    serial: str | None = None


@dataclass(frozen=True)
class AdapterRef:
    """Which adapter implements this robot, and its adapter-specific configuration."""

    kind: str
    profile: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


@contract("component_manifest", version="1.0")
class ComponentManifest(Contract):
    robot_id: str
    name: str
    hardware_revision: str
    adapter: AdapterRef
    components: tuple[ComponentSpec, ...] = ()
    nominal_capabilities: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()
    description: str = ""

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
