"""Minimal units and coordinate-frame support.

This is intentionally small: enough to make units explicit in every contract and to
reject dimensionally wrong comparisons. Integrations that need a full unit system can
convert at their boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import FrameError, UnitError

# unit -> (dimension, scale to the SI base unit of that dimension)
_UNITS: dict[str, tuple[str, float]] = {
    "1": ("dimensionless", 1.0),
    "%": ("dimensionless", 0.01),
    "m": ("length", 1.0),
    "mm": ("length", 1e-3),
    "cm": ("length", 1e-2),
    "s": ("time", 1.0),
    "ms": ("time", 1e-3),
    "us": ("time", 1e-6),
    "Hz": ("frequency", 1.0),
    "rad": ("angle", 1.0),
    "deg": ("angle", 3.141592653589793 / 180.0),
    "m/s": ("velocity", 1.0),
    "rad/s": ("angular_velocity", 1.0),
    "kg": ("mass", 1.0),
    "g": ("mass", 1e-3),
    "N": ("force", 1.0),
    "N*m": ("torque", 1.0),
    "W": ("power", 1.0),
    "V": ("voltage", 1.0),
    "A": ("current", 1.0),
    "J": ("energy", 1.0),
    "Wh": ("energy", 3600.0),
    "degC": ("temperature", 1.0),
    "B": ("data", 1.0),
    "MB": ("data", 1e6),
}


def register_unit(unit: str, dimension: str, scale: float) -> None:
    """Extension point for integrations with additional units."""
    existing = _UNITS.get(unit)
    if existing and existing != (dimension, scale):
        raise UnitError("unit already registered with a different meaning", unit=unit)
    _UNITS[unit] = (dimension, scale)


def dimension_of(unit: str) -> str:
    try:
        return _UNITS[unit][0]
    except KeyError:
        raise UnitError("unknown unit", unit=unit) from None


def convert(value: float, from_unit: str, to_unit: str) -> float:
    if from_unit == to_unit:
        return value
    d1, s1 = _UNITS.get(from_unit, (None, 0.0))
    d2, s2 = _UNITS.get(to_unit, (None, 0.0))
    if d1 is None or d2 is None:
        raise UnitError("unknown unit", from_unit=from_unit, to_unit=to_unit)
    if d1 != d2:
        raise UnitError("incompatible dimensions", from_unit=from_unit, to_unit=to_unit)
    if d1 == "temperature":
        raise UnitError("temperature conversion is not linear; convert explicitly")
    return value * s1 / s2


@dataclass(frozen=True)
class Quantity:
    value: float
    unit: str

    def __post_init__(self) -> None:
        dimension_of(self.unit)

    def to(self, unit: str) -> Quantity:
        return Quantity(convert(self.value, self.unit, unit), unit)


@dataclass(frozen=True)
class Pose2D:
    """A planar pose expressed in a named coordinate frame."""

    frame: str
    x: float
    y: float
    theta: float = 0.0

    def require_frame(self, frame: str) -> None:
        if self.frame != frame:
            raise FrameError("pose is in an unexpected frame", expected=frame, actual=self.frame)
