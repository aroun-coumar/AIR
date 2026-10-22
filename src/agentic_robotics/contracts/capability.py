"""Capability reports (baseline sections 6 and 15).

A nominal capability from a manifest says what a robot was built to do. A
:class:`CapabilityReport` says what it can do *now*, with limiting factors and the age
of the evidence. The runtime admits commands against the report, not the manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..core.schema import Contract, contract

__requirements__ = ("REQ01", "REQ13")


class ProfileStatus(StrEnum):
    AVAILABLE = "available"
    RESTRICTED = "restricted"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class CapabilityProfile:
    name: str
    status: ProfileStatus
    limits: dict[str, float] = field(default_factory=dict)
    limiting_factors: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


@contract("capability_report", version="1.0")
class CapabilityReport(Contract):
    robot_id: str
    capability_revision: int
    observed_at: float
    clock_domain: str
    profiles: tuple[CapabilityProfile, ...] = ()

    def get(self, name: str) -> CapabilityProfile | None:
        for p in self.profiles:
            if p.name == name:
                return p
        return None

    def is_usable(self, name: str) -> bool:
        p = self.get(name)
        return p is not None and p.status is not ProfileStatus.UNAVAILABLE

    def age(self, now: float) -> float:
        return now - self.observed_at
