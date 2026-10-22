"""Typed parameter metadata and parameter sets (ADR 0016, W5.1).

The metadata pattern (display name, description, SI unit from a fixed table, range,
enumerated values or bitmask, reboot-required, calibration flag, user level) follows
ArduPilot's parameter documentation. ArduPilot is GPL-3.0; this is a clean-room
implementation of the idea, with no code or text copied. Units come from AIR's own unit
table (:func:`agentic_robotics.core.units.known_units`).

    schema = ParameterSchema(name="drive.v1", parameters=(...))
    problems = schema.validate(param_set)
    changes = schema.diff(old_set, new_set)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from ..core.errors import SchemaError
from ..core.ids import is_valid_id
from ..core.schema import Contract, contract
from ..core.units import known_units

__requirements__ = ("REQ03", "REQ04", "REQ16", "F02", "F04", "G10")

ParamValue = bool | int | float | str


class ParamType(StrEnum):
    FLOAT = "float"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"          # one of ``values``
    BITMASK = "bitmask"    # integer whose set bits must be named in ``bitmask``
    STRING = "string"


class UserLevel(StrEnum):
    STANDARD = "standard"
    ADVANCED = "advanced"
    DEVELOPER = "developer"


@dataclass(frozen=True)
class ParameterDef:
    name: str
    type: ParamType
    display_name: str = ""
    description: str = ""
    unit: str | None = None               # from core.units.known_units()
    min: float | None = None
    max: float | None = None
    increment: float | None = None
    values: dict[str, str] = field(default_factory=dict)    # value -> label (ENUM)
    bitmask: dict[str, str] = field(default_factory=dict)   # bit index -> label
    default: ParamValue | None = None
    reboot_required: bool = False
    calibration: bool = False             # produced by calibration; do not hand-edit
    read_only: bool = False
    user_level: UserLevel = UserLevel.STANDARD

    def __post_init__(self) -> None:
        if not is_valid_id(self.name):
            raise SchemaError("invalid parameter name", name=self.name)
        if self.unit is not None and self.unit not in known_units():
            raise SchemaError("unit is not in AIR's unit table", parameter=self.name,
                              unit=self.unit)
        if self.min is not None and self.max is not None and self.min > self.max:
            raise SchemaError("parameter min exceeds max", parameter=self.name)
        if self.type is ParamType.ENUM and not self.values:
            raise SchemaError("enum parameter needs values", parameter=self.name)
        if self.type is ParamType.BITMASK:
            if not self.bitmask:
                raise SchemaError("bitmask parameter needs named bits", parameter=self.name)
            for bit in self.bitmask:
                if not bit.isdigit() or int(bit) > 63:
                    raise SchemaError("bitmask keys are bit indexes 0..63",
                                      parameter=self.name, bit=bit)
        if self.default is not None:
            problem = self.check(self.default)
            if problem:
                raise SchemaError("default is invalid", parameter=self.name, problem=problem)

    def check(self, value: ParamValue) -> str | None:
        """Return why ``value`` is not acceptable, or None."""
        t = self.type
        if t is ParamType.BOOL:
            return None if isinstance(value, bool) else "expected a boolean"
        if t is ParamType.STRING:
            return None if isinstance(value, str) else "expected a string"
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return "expected a number"
        if t in (ParamType.INT, ParamType.BITMASK, ParamType.ENUM) and (
                isinstance(value, float) and not value.is_integer()):
            return "expected an integer"
        if not math.isfinite(value):
            return "value is not finite"
        if self.min is not None and value < self.min:
            return f"below minimum {self.min:g}"
        if self.max is not None and value > self.max:
            return f"above maximum {self.max:g}"
        if t is ParamType.ENUM and str(int(value)) not in self.values:
            return f"not one of {sorted(self.values, key=int)}"
        if t is ParamType.BITMASK:
            v = int(value)
            if v < 0:
                return "bitmask is negative"
            allowed = sum(1 << int(b) for b in self.bitmask)
            if v & ~allowed:
                return f"sets unnamed bits {bin(v & ~allowed)}"
        return None

    def label(self, value: ParamValue) -> str:
        if self.type is ParamType.ENUM and isinstance(value, (int, float)):
            return self.values.get(str(int(value)), str(value))
        if self.type is ParamType.BITMASK and isinstance(value, (int, float)):
            v = int(value)
            return "|".join(lbl for b, lbl in sorted(self.bitmask.items(), key=lambda x:
                                                       int(x[0])) if v & (1 << int(b))) or "0"
        return f"{value}{' ' + self.unit if self.unit else ''}"


@dataclass(frozen=True)
class ParamChange:
    name: str
    old: ParamValue | None
    new: ParamValue | None
    reboot_required: bool = False
    calibration: bool = False
    known: bool = True

    def __str__(self) -> str:
        flags = ("" if self.known else " [unknown parameter]") + \
            (" [reboot]" if self.reboot_required else "") + \
            (" [calibration]" if self.calibration else "")
        return f"{self.name}: {self.old!r} -> {self.new!r}{flags}"


@contract("parameter_schema", version="1.0")
class ParameterSchema(Contract):
    """The configuration schema of an adapter, component or firmware."""

    name: str
    parameters: tuple[ParameterDef, ...]
    applies_to: str = ""                 # adapter kind, catalog type id or firmware name
    description: str = ""

    def __post_init__(self) -> None:
        names = [p.name for p in self.parameters]
        if len(set(names)) != len(names):
            raise SchemaError("duplicate parameter names", schema=self.name)

    def get(self, name: str) -> ParameterDef | None:
        for p in self.parameters:
            if p.name == name:
                return p
        return None

    def validate(self, pset: ParameterSet, *, allow_unknown: bool = False) -> list[str]:
        problems = []
        if pset.schema_name and pset.schema_name != self.name:
            problems.append(f"set is for schema {pset.schema_name!r}, not {self.name!r}")
        for name, value in sorted(pset.values.items()):
            p = self.get(name)
            if p is None:
                if not allow_unknown:
                    problems.append(f"{name}: unknown parameter")
                continue
            why = p.check(value)
            if why:
                problems.append(f"{name}: {why}")
        return problems

    def diff(self, old: ParameterSet, new: ParameterSet) -> list[ParamChange]:
        out = []
        for name in sorted(set(old.values) | set(new.values)):
            a, b = old.values.get(name), new.values.get(name)
            if a == b and type(a) is type(b):
                continue
            p = self.get(name)
            out.append(ParamChange(name, a, b, reboot_required=bool(p and p.reboot_required),
                                   calibration=bool(p and p.calibration), known=p is not None))
        return out

    def defaults(self, robot_id: str) -> ParameterSet:
        return ParameterSet(robot_id=robot_id, schema_name=self.name,
                            values={p.name: p.default for p in self.parameters
                                    if p.default is not None})


@contract("parameter_set", version="1.0")
class ParameterSet(Contract):
    """A robot's parameter values at one time (a snapshot or a proposed change)."""

    robot_id: str
    values: dict[str, ParamValue]
    schema_name: str = ""
    captured_at: float | None = None
    source: str = ""                     # e.g. "read from device", "proposed by agent"
