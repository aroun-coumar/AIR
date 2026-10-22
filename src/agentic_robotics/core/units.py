"""Minimal units and coordinate-frame support.

This is intentionally small: enough to make units explicit in every contract and to
reject dimensionally wrong comparisons. Integrations that need a full unit system can
convert at their boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import FrameError, UnitError

__requirements__ = ("REQ01",)

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
    # Hardware catalog units (device properties and telemetry).
    "km": ("length", 1e3),
    "um": ("length", 1e-6),
    "nm": ("length", 1e-9),
    "m^2": ("area", 1.0),
    "cm^2": ("area", 1e-4),
    "m^3": ("volume", 1.0),
    "L": ("volume", 1e-3),
    "mL": ("volume", 1e-6),
    "min": ("time", 60.0),
    "h": ("time", 3600.0),
    "kHz": ("frequency", 1e3),
    "MHz": ("frequency", 1e6),
    "GHz": ("frequency", 1e9),
    "rpm": ("angular_velocity", 2 * 3.141592653589793 / 60.0),
    "deg/s": ("angular_velocity", 3.141592653589793 / 180.0),
    "rad/s^2": ("angular_acceleration", 1.0),
    "m/s^2": ("acceleration", 1.0),
    "g0": ("acceleration", 9.80665),
    "km/h": ("velocity", 1 / 3.6),
    "mN": ("force", 1e-3),
    "kN": ("force", 1e3),
    "mN*m": ("torque", 1e-3),
    "N/m": ("stiffness", 1.0),
    "N*m/rad": ("rotational_stiffness", 1.0),
    "kg*m^2": ("inertia", 1.0),
    "Pa": ("pressure", 1.0),
    "kPa": ("pressure", 1e3),
    "MPa": ("pressure", 1e6),
    "bar": ("pressure", 1e5),
    "L/min": ("flow", 1e-3 / 60.0),
    "m^3/h": ("flow", 1 / 3600.0),
    "mW": ("power", 1e-3),
    "kW": ("power", 1e3),
    "mV": ("voltage", 1e-3),
    "kV": ("voltage", 1e3),
    "uA": ("current", 1e-6),
    "mA": ("current", 1e-3),
    "Ah": ("charge", 3600.0),
    "mAh": ("charge", 3.6),
    "kWh": ("energy", 3.6e6),
    "Wh/kg": ("specific_energy", 3600.0),
    "Ohm": ("resistance", 1.0),
    "mOhm": ("resistance", 1e-3),
    "kOhm": ("resistance", 1e3),
    "F": ("capacitance", 1.0),
    "uF": ("capacitance", 1e-6),
    "H": ("inductance", 1.0),
    "mH": ("inductance", 1e-3),
    "T": ("magnetic_flux_density", 1.0),
    "mT": ("magnetic_flux_density", 1e-3),
    "uT": ("magnetic_flux_density", 1e-6),
    "lx": ("illuminance", 1.0),
    "lm": ("luminous_flux", 1.0),
    "W/m^2": ("irradiance", 1.0),
    "dB": ("level", 1.0),
    "dBm": ("power_level", 1.0),
    "dBA": ("sound_level", 1.0),
    "ppm": ("dimensionless", 1e-6),
    "ppb": ("dimensionless", 1e-9),
    "%RH": ("relative_humidity", 0.01),
    "K": ("temperature", 1.0),
    "ug/m^3": ("mass_concentration", 1e-9),
    "bit/s": ("data_rate", 1.0),
    "kbit/s": ("data_rate", 1e3),
    "Mbit/s": ("data_rate", 1e6),
    "Gbit/s": ("data_rate", 1e9),
    "GB": ("data", 1e9),
    "TOPS": ("compute", 1e12),
    "TFLOPS": ("compute_float", 1e12),
    "px": ("pixels", 1.0),
    "fps": ("frequency", 1.0),
    "pH": ("acidity", 1.0),
    "uS/cm": ("conductivity", 1e-4),
    "cycles": ("count", 1.0),
}

#: Units a hardware catalog may use, listed for catalog authors and validators.
def known_units() -> tuple[str, ...]:
    return tuple(_UNITS)


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
