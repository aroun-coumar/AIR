"""Shared core: identities, schemas, units, frames, clocks and errors."""

from .clock import Clock, ClockStatus, ManualClock, MonotonicClock, SyncState
from .errors import (
    AdmissionRejected,
    AuthorityError,
    FrameError,
    FrameworkError,
    IncompatibleSchemaVersion,
    InvalidTransition,
    OutcomeNotVerified,
    PlanError,
    PluginError,
    ResourceConflict,
    SchemaError,
    UnitError,
    UnsupportedOperation,
)
from .ids import is_valid_id, new_id
from .schema import Contract, Meta, contract, load_any, registered_contracts
from .units import Pose2D, Quantity, convert, dimension_of, register_unit

__all__ = [
    "AdmissionRejected",
    "AuthorityError",
    "Clock",
    "ClockStatus",
    "Contract",
    "FrameError",
    "FrameworkError",
    "IncompatibleSchemaVersion",
    "InvalidTransition",
    "ManualClock",
    "Meta",
    "MonotonicClock",
    "OutcomeNotVerified",
    "PlanError",
    "PluginError",
    "Pose2D",
    "Quantity",
    "ResourceConflict",
    "SchemaError",
    "SyncState",
    "UnitError",
    "UnsupportedOperation",
    "contract",
    "convert",
    "dimension_of",
    "is_valid_id",
    "load_any",
    "new_id",
    "register_unit",
    "registered_contracts",
]
