"""Maintenance: prognostics feeding capability, readiness and triage (ADR 0015)."""

from .prognostics import (
    Interval,
    LinearDegradationEstimator,
    LoadingAssumption,
    PrognosticCheck,
    PrognosticEstimate,
    TimeOfEvent,
    estimates_from_ledger,
    prediction_error,
    prognostic_gater,
    record_estimate,
    threshold_crossings,
)

__requirements__ = ("REQ16",)

__all__ = [
    "Interval", "LinearDegradationEstimator", "LoadingAssumption", "PrognosticCheck",
    "PrognosticEstimate", "TimeOfEvent", "estimates_from_ledger", "prediction_error",
    "prognostic_gater", "record_estimate", "threshold_crossings",
]
