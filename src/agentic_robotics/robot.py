"""A Robot: a physical or simulated system identified by its manifest and served by an
adapter. A robot is not an agent; agents decide, robots execute (baseline section 6)."""

from __future__ import annotations

from pathlib import Path

from .contracts.capability import CapabilityReport
from .contracts.manifest import ComponentManifest
from .contracts.observation import Observation
from .plugins import RobotAdapter, create_adapter


class Robot:
    def __init__(self, manifest: ComponentManifest, adapter: RobotAdapter) -> None:
        self.manifest = manifest
        self.adapter = adapter

    @classmethod
    def from_manifest(cls, path_or_manifest: str | Path | ComponentManifest) -> Robot:
        manifest = (path_or_manifest if isinstance(path_or_manifest, ComponentManifest)
                    else ComponentManifest.load(path_or_manifest))
        return cls(manifest, create_adapter(manifest))

    @property
    def robot_id(self) -> str:
        return self.manifest.robot_id

    def capability_report(self) -> CapabilityReport:
        return self.adapter.capability_report()

    def observe(self) -> dict[str, Observation]:
        return self.adapter.observe()

    def __repr__(self) -> str:
        return f"Robot({self.robot_id!r}, adapter={self.adapter.kind!r})"
