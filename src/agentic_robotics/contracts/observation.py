"""Observation contract (baseline section 6)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from ..core.units import dimension_of

__requirements__ = ("REQ01", "REQ12")


class Quality(StrEnum):
    GOOD = "good"
    DEGRADED = "degraded"
    INVALID = "invalid"


@contract("observation", version="1.0")
class Observation(Contract):
    """A measurement or artifact reference with its time, frame, quality and uncertainty.

    Acquisition time and receipt time are kept separately so that data age can be
    computed from when the world was measured, not when the message arrived.
    """

    source: str
    signal: str
    clock_domain: str
    acquired_at: float
    received_at: float
    value: Any = None
    unit: str | None = None
    frame: str | None = None
    spatial: bool = False
    quality: Quality = Quality.GOOD
    uncertainty: float | None = None
    artifact_ref: str | None = None

    def __post_init__(self) -> None:
        if self.unit is not None:
            dimension_of(self.unit)
        if self.spatial and not self.frame:
            raise SchemaError("spatial observations require a frame", signal=self.signal)
        if self.received_at < self.acquired_at:
            raise SchemaError("received_at precedes acquired_at", signal=self.signal)

    def age(self, now: float) -> float:
        return now - self.acquired_at
