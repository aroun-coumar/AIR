"""Shared meaning for catalog data: vocabulary links, signal binding, WoT and JSON-LD export.

AIR ids (``sensor.lidar.safety_scanner``) mean something only to AIR. This module links
them to vocabularies other systems already understand, without inventing identifiers:

- units and quantity kinds: QUDT (``unit:V``, ``quantitykind:Voltage``)
- what a component is: W3C SOSA/SSN (``sosa:Sensor``, ``sosa:Actuator``, ``sosa:Platform``)
  and ETSI SAREF (``saref:Device``, ``saref:Sensor``, ``saref:Actuator``)
- registry equivalents, parsed from catalog references: OMA LwM2M object URNs
  (``urn:oma:lwm2m:ext:3303``) and CSA Matter device type ids, kept as structured
  ``{"scheme", "id"}`` pairs because Matter defines no IRIs
- AIR's own terms (relations, safety relevance) under the ``air:`` namespace

Exports: a W3C WoT **Thing Model** per component type (a Thing Model needs no protocol
bindings, which a manifest does not have), and a JSON-LD graph of a robot manifest
(platform, hosted components, typed relations, observable properties with units).

``bind_observation`` ties a runtime observation name (``<component_id>.<signal>``) to its
component, catalog signal, unit and effective limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..contracts.manifest import ComponentManifest
from ..core.errors import UnitError
from ..core.units import convert, dimension_of
from .catalog import DeviceCatalog, DeviceType, SignalSpec
from .validate import ComponentLimit, evaluate, limit_for

__requirements__ = ("REQ03", "REQ01", "D16", "D17")

CONTEXT: dict[str, str] = {
    "sosa": "http://www.w3.org/ns/sosa/",
    "ssn": "http://www.w3.org/ns/ssn/",
    "saref": "https://saref.etsi.org/core/",
    "qudt": "http://qudt.org/schema/qudt/",
    "unit": "http://qudt.org/vocab/unit/",
    "quantitykind": "http://qudt.org/vocab/quantitykind/",
    "schema": "https://schema.org/",
    "air": "https://raw.githubusercontent.com/aroun-coumar/AIR/main/vocab#",
}
WOT_TD_CONTEXT = "https://www.w3.org/2022/wot/td/v1.1"

#: AIR unit -> QUDT unit local name. Units with no confident QUDT equivalent are absent
#: and exported as literals (``air:unit``) instead of a guessed IRI.
QUDT_UNITS: dict[str, str] = {
    "1": "UNITLESS", "%": "PERCENT", "m": "M", "mm": "MilliM", "cm": "CentiM", "km": "KiloM",
    "um": "MicroM", "nm": "NanoM", "s": "SEC", "ms": "MilliSEC", "us": "MicroSEC",
    "min": "MIN", "h": "HR", "Hz": "HZ", "kHz": "KiloHZ", "MHz": "MegaHZ", "GHz": "GigaHZ",
    "rad": "RAD", "deg": "DEG", "m/s": "M-PER-SEC", "km/h": "KiloM-PER-HR",
    "rad/s": "RAD-PER-SEC", "deg/s": "DEG-PER-SEC", "rpm": "REV-PER-MIN",
    "rad/s^2": "RAD-PER-SEC2", "m/s^2": "M-PER-SEC2", "g0": "G", "kg": "KiloGM", "g": "GM",
    "N": "N", "mN": "MilliN", "kN": "KiloN", "N*m": "N-M", "mN*m": "MilliN-M",
    "N/m": "N-PER-M", "N*m/rad": "N-M-PER-RAD", "kg*m^2": "KiloGM-M2", "W": "W",
    "mW": "MilliW", "kW": "KiloW", "V": "V", "mV": "MilliV", "kV": "KiloV", "A": "A",
    "mA": "MilliA", "uA": "MicroA", "J": "J", "Wh": "W-HR", "kWh": "KiloW-HR",
    "Ah": "A-HR", "mAh": "MilliA-HR", "Ohm": "OHM", "mOhm": "MilliOHM", "kOhm": "KiloOHM",
    "F": "FARAD", "uF": "MicroFARAD", "H": "H", "mH": "MilliH", "T": "T", "mT": "MilliT",
    "uT": "MicroT", "degC": "DEG_C", "K": "K", "Pa": "PA", "kPa": "KiloPA", "MPa": "MegaPA",
    "bar": "BAR", "L": "L", "mL": "MilliL", "m^2": "M2", "cm^2": "CentiM2", "m^3": "M3",
    "L/min": "L-PER-MIN", "m^3/h": "M3-PER-HR", "lx": "LUX", "lm": "LM",
    "W/m^2": "W-PER-M2", "ppm": "PPM", "ppb": "PPB", "%RH": "PERCENT_RH", "B": "BYTE",
    "MB": "MegaBYTE", "GB": "GigaBYTE", "bit/s": "BIT-PER-SEC", "kbit/s": "KiloBIT-PER-SEC",
    "Mbit/s": "MegaBIT-PER-SEC", "Gbit/s": "GigaBIT-PER-SEC", "dB": "DeciB",
    "ug/m^3": "MicroGM-PER-M3", "pH": "PH",
}

#: AIR dimension -> QUDT quantity kind local name (only where the mapping is exact).
QUANTITY_KINDS: dict[str, str] = {
    "length": "Length", "time": "Time", "frequency": "Frequency", "angle": "PlaneAngle",
    "velocity": "Velocity", "angular_velocity": "AngularVelocity",
    "angular_acceleration": "AngularAcceleration", "acceleration": "Acceleration",
    "mass": "Mass", "force": "Force", "torque": "Torque", "power": "Power",
    "voltage": "Voltage", "current": "ElectricCurrent", "energy": "Energy",
    "charge": "ElectricCharge", "temperature": "Temperature", "pressure": "Pressure",
    "area": "Area", "volume": "Volume", "flow": "VolumeFlowRate",
    "resistance": "Resistance", "capacitance": "Capacitance", "inductance": "Inductance",
    "magnetic_flux_density": "MagneticFluxDensity", "illuminance": "Illuminance",
    "luminous_flux": "LuminousFlux", "irradiance": "Irradiance",
    "relative_humidity": "RelativeHumidity", "inertia": "MomentOfInertia",
    "dimensionless": "Dimensionless",
}

_SENSING = {"sensor"}
_ACTING = {"actuator", "end_effector"}


def type_classes(t: DeviceType) -> list[str]:
    """Vocabulary classes a device type is an instance of, plus its own ``semantic`` list."""
    out = ["saref:Device"]
    if t.category in _SENSING:
        out += ["sosa:Sensor", "saref:Sensor"]
    if t.category in _ACTING:
        out += ["sosa:Actuator", "saref:Actuator"]
    out.append(f"air:{t.type_id}")
    return list(dict.fromkeys([*out, *t.semantic]))


_LWM2M = re.compile(r"LwM2M\b.*?\bobject\s+(\d+)", re.I)
_MATTER = re.compile(r"Matter\s+([\d.]+)\s+device type\s+(0x[0-9A-Fa-f]{4})", re.I)


def lwm2m_urn(object_id: int) -> str:
    """OMA LwM2M object URN by registry range (OMA, external SDO such as IPSO, vendor)."""
    if object_id < 1024:
        return f"urn:oma:lwm2m:oma:{object_id}"
    if object_id <= 10240:
        return f"urn:oma:lwm2m:ext:{object_id}"
    return f"urn:oma:lwm2m:x:{object_id}"


def equivalents(t: DeviceType) -> list[dict[str, str]]:
    """Registry entries this type corresponds to, parsed from its references."""
    out: list[dict[str, str]] = []
    for r in t.references:
        if m := _LWM2M.search(r.name):
            oid = int(m.group(1))
            out.append({"scheme": "lwm2m.object", "id": str(oid), "urn": lwm2m_urn(oid),
                        "label": r.name})
        elif m := _MATTER.search(r.name):
            out.append({"scheme": "matter.device_type", "id": m.group(2).lower(),
                        "spec_version": m.group(1), "label": r.name})
    return out


def unit_iri(unit: str | None) -> str | None:
    if unit is None or unit not in QUDT_UNITS:
        return None
    return f"unit:{QUDT_UNITS[unit]}"


def quantity_kind(unit: str | None) -> str | None:
    if unit is None:
        return None
    try:
        kind = QUANTITY_KINDS.get(dimension_of(unit))
    except UnitError:
        return None
    return f"quantitykind:{kind}" if kind else None


def _unit_annotation(unit: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if unit is None:
        return out
    iri = unit_iri(unit)
    out["unit"] = iri or unit
    if iri is None:
        out["air:unit"] = unit
    if qk := quantity_kind(unit):
        out["qudt:hasQuantityKind"] = qk
    return out


_NUMERIC = {"quantity": "number", "number": "number", "integer": "integer",
            "bool": "boolean", "string": "string", "enum": "string"}


def thing_model(t: DeviceType) -> dict[str, Any]:
    """A W3C WoT Thing Model (TD 1.1 context) for a device type.

    Datasheet properties become read-only affordances; telemetry signals become observable
    read-only affordances with QUDT units and AIR's typical limits as ``air:`` annotations.
    """
    props: dict[str, Any] = {}
    for p in t.properties:
        a: dict[str, Any] = {"type": _NUMERIC[p.kind.value], "readOnly": True,
                             "description": p.description or p.name,
                             "air:required": p.required}
        if p.values:
            a["enum"] = list(p.values)
        a.update(_unit_annotation(p.unit))
        props[p.name] = a
    for s in t.telemetry:
        a = {"type": "number", "readOnly": True, "observable": True,
             "description": s.description or s.name, "@type": "sosa:ObservableProperty"}
        a.update(_unit_annotation(s.unit))
        limits = {k: v for k, v in (("air:warnAbove", s.warn_above),
                                    ("air:criticalAbove", s.critical_above),
                                    ("air:warnBelow", s.warn_below),
                                    ("air:criticalBelow", s.critical_below)) if v is not None}
        a.update(limits)
        props.setdefault(s.name, a)
    tm: dict[str, Any] = {
        "@context": [WOT_TD_CONTEXT, CONTEXT],
        "@type": ["tm:ThingModel", *type_classes(t)],
        "title": t.name,
        "description": t.description,
        "air:typeId": t.type_id,
        "air:safetyRelevant": t.safety_relevant,
        "air:domains": list(t.domains),
        "air:interfaces": list(t.interfaces),
        "properties": props,
    }
    if eq := equivalents(t):
        tm["air:equivalents"] = eq
    if t.failure_modes:
        tm["air:failureModes"] = list(t.failure_modes)
    if t.extends:
        tm["links"] = [{"rel": "tm:extends", "href": f"#{t.extends}",
                        "type": "application/tm+json"}]
    return tm


def manifest_graph(manifest: ComponentManifest, catalog: DeviceCatalog) -> dict[str, Any]:
    """JSON-LD: the robot as a ``sosa:Platform`` hosting its components, with typed
    relations, units and the component properties the manifest states."""
    base = f"urn:air:robot:{manifest.robot_id}"
    nodes: list[dict[str, Any]] = [{
        "@id": base, "@type": ["sosa:Platform", "ssn:System",
                               *( [f"air:{manifest.robot_class}"] if manifest.robot_class
                                  else [])],
        "schema:name": manifest.name,
        "sosa:hosts": [{"@id": f"{base}/{c.component_id}"} for c in manifest.components
                       if c.parent is None],
    }]
    for c in manifest.components:
        t = catalog.type(c.type_id) if c.type_id else None
        node: dict[str, Any] = {
            "@id": f"{base}/{c.component_id}",
            "@type": type_classes(t) if t else ["saref:Device"],
            "schema:name": c.kind,
            "air:sensitivity": c.sensitivity.value,
        }
        if c.vendor:
            node["schema:manufacturer"] = c.vendor
        if c.model:
            node["schema:model"] = c.model
        if c.serial:
            node["schema:serialNumber"] = c.serial
        if c.parent:
            node["air:partOf"] = {"@id": f"{base}/{c.parent}"}
        for r in c.relations:
            node.setdefault(f"air:{r.kind.value}", []).append({"@id": f"{base}/{r.target}"})
        if t is not None:
            node["sosa:observes"] = [
                {"@id": f"{base}/{c.component_id}#{s.name}", "@type": "sosa:ObservableProperty",
                 **({"qudt:hasQuantityKind": qk} if (qk := quantity_kind(s.unit)) else {})}
                for s in t.telemetry]
        values = []
        for name, value in c.properties.items():
            spec = t.property(name) if t else None
            v: dict[str, Any] = {"schema:name": name}
            if isinstance(value, dict) and set(value) == {"value", "unit"}:
                v["schema:value"] = value["value"]
                v.update(_unit_annotation(value["unit"]))
            else:
                v["schema:value"] = value
                if spec is not None:
                    v.update(_unit_annotation(spec.unit))
            values.append(v)
        if values:
            node["schema:additionalProperty"] = values
        nodes.append(node)
    return {"@context": CONTEXT, "@graph": nodes}


@dataclass(frozen=True)
class BoundObservation:
    name: str
    component_id: str
    type_id: str
    signal: SignalSpec
    limit: ComponentLimit | None
    value: float | None = None
    unit: str | None = None
    status: str | None = None          # ok | warn | critical, when a value was given
    problem: str | None = None         # unit mismatch and similar


def bind_observation(manifest: ComponentManifest, catalog: DeviceCatalog, name: str,
                     value: float | None = None, unit: str | None = None
                     ) -> BoundObservation | None:
    """Resolve ``<component_id>.<signal>`` to its catalog signal and effective limits, and
    evaluate a reading (converted to the signal's unit) when one is given."""
    component_id, _, signal_name = name.rpartition(".")
    c = manifest.component(component_id)
    t = catalog.type(c.type_id) if c and c.type_id else None
    s = t.signal(signal_name) if t else None
    if c is None or t is None or s is None:
        return None
    lim = limit_for(manifest, catalog, component_id, signal_name)
    if value is None:
        return BoundObservation(name, component_id, t.type_id, s, lim)
    reading, problem = value, None
    if unit and s.unit and unit != s.unit:
        try:
            reading = convert(value, unit, s.unit)
        except UnitError as exc:
            problem = f"cannot compare {unit} with {s.unit}: {exc.message}"
    status = evaluate(lim.signal if lim else s, reading) if problem is None else None
    return BoundObservation(name, component_id, t.type_id, s, lim, reading, s.unit or unit,
                            status, problem)
