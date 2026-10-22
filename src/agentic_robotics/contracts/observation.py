"""Observation contract (baseline section 6)."""

from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from ..core.units import dimension_of

__requirements__ = ("REQ01", "REQ12", "REQ14")


class Quality(StrEnum):
    GOOD = "good"
    DEGRADED = "degraded"
    INVALID = "invalid"


class Origin(StrEnum):
    """How a value came to be. Only ``measured`` and ``estimated`` values are facts about
    the world; a ``model`` judgment or a ``reported`` claim is evidence, never a verdict."""

    MEASURED = "measured"      # read from a sensor or device state
    ESTIMATED = "estimated"    # derived by an estimator; must carry its uncertainty
    MODEL = "model"            # an LLM/VLM/classifier judgment
    REPORTED = "reported"      # something a participant said happened


class AssociationQuality(StrEnum):
    """How surely an observation is about *this* command's effect (PhyAgentOS
    ``association_quality``). Only ``authoritative`` evidence can satisfy a
    postcondition."""

    AUTHORITATIVE = "authoritative"
    BEST_EFFORT = "best_effort"


@contract("observation", version="1.1")
class Observation(Contract):
    """A measurement or artifact reference with its time, frame, quality and uncertainty.

    Acquisition time and receipt time are kept separately so that data age can be
    computed from when the world was measured, not when the message arrived.

    1.1 adds ``origin``, ``association_quality``, ``covariance`` (row-major, square)
    and ``score`` (0..1). An ``estimated`` observation must carry its uncertainty
    (``uncertainty``, ``covariance`` or ``score``).
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
    origin: Origin = Origin.MEASURED
    association_quality: AssociationQuality = AssociationQuality.AUTHORITATIVE
    covariance: tuple[float, ...] | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        if self.unit is not None:
            dimension_of(self.unit)
        if self.spatial and not self.frame:
            raise SchemaError("spatial observations require a frame", signal=self.signal)
        if self.received_at < self.acquired_at:
            raise SchemaError("received_at precedes acquired_at", signal=self.signal)
        if self.covariance is not None:
            n = math.isqrt(len(self.covariance))
            if not self.covariance or n * n != len(self.covariance):
                raise SchemaError("covariance must be a non-empty square matrix",
                                  signal=self.signal, length=len(self.covariance))
            if not all(math.isfinite(v) for v in self.covariance):
                raise SchemaError("covariance must be finite", signal=self.signal)
            if any(self.covariance[i * n + i] < 0 for i in range(n)):
                raise SchemaError("covariance diagonal must be non-negative",
                                  signal=self.signal)
        if self.score is not None and not (0.0 <= self.score <= 1.0):
            raise SchemaError("score must lie in [0, 1]", signal=self.signal, score=self.score)
        if self.uncertainty is not None and not (
                math.isfinite(self.uncertainty) and self.uncertainty >= 0):
            raise SchemaError("uncertainty must be finite and non-negative",
                              signal=self.signal)
        if self.origin is Origin.ESTIMATED and self.uncertainty is None \
                and self.covariance is None and self.score is None:
            raise SchemaError("an estimate must carry its uncertainty (uncertainty, "
                              "covariance or score)", signal=self.signal)

    def age(self, now: float) -> float:
        return now - self.acquired_at

    @property
    def authoritative(self) -> bool:
        """True when this observation may satisfy a postcondition: a measured or
        estimated fact, authoritatively associated, with usable quality."""
        return (self.origin in (Origin.MEASURED, Origin.ESTIMATED)
                and self.association_quality is AssociationQuality.AUTHORITATIVE
                and self.quality is not Quality.INVALID)
