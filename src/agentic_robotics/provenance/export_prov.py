"""W3C PROV-JSON export of a project ledger.

PROV-JSON is a W3C Member Submission (2013), not a Recommendation, but it is what the
``prov`` Python package, ProvToolbox and ProvStore read. The mapping:

=========================  ==========================================================
AIR ledger                 PROV
=========================  ==========================================================
``entity.registered``      ``entity`` (``prov:type`` = AIR kind, digest, uri, ...)
``activity.started/...``   ``activity`` with ``prov:startTime`` / ``prov:endTime``
activity ``actors``        ``agent`` + ``wasAssociatedWith``
``activity.used``          ``used``
``activity.generated``     ``wasGeneratedBy``; ``derived_from`` -> ``wasDerivedFrom``
``entity.replaced``        ``wasInvalidatedBy`` (the removed part, by the activity)
activity ``parent_id``     ``wasInformedBy``
=========================  ==========================================================

An export is a lossy *view*; the ledger stays the source of truth. Activity parameters
and notes are not exported (they may hold content that must not leave the project).
"""

from __future__ import annotations

import datetime
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .project import Project

__requirements__ = ("REQ10", "F07")

PREFIXES = {
    "air": "https://agentic-robotics.invalid/ns/prov#",
    "ent": "urn:air:entity:",
    "act": "urn:air:activity:",
    "agt": "urn:air:agent:",
}
_UNSAFE = re.compile(r"[^A-Za-z0-9._\-:/@~]")


def qname(prefix: str, local: str) -> str:
    """A PROV qualified name; characters outside PROV-N's safe set are %-encoded."""
    return f"{prefix}:" + _UNSAFE.sub(lambda m: "".join(f"%{b:02X}" for b in
                                                         m.group().encode()), local)


def _time(ts: float | None) -> dict[str, str] | None:
    if ts is None:
        return None
    value = datetime.datetime.fromtimestamp(ts, datetime.UTC).isoformat()
    return {"$": value, "type": "xsd:dateTime"}


def to_prov_json(project: Project) -> dict[str, Any]:
    doc: dict[str, Any] = {"prefix": dict(PREFIXES), "entity": {}, "activity": {},
                           "agent": {}, "used": {}, "wasGeneratedBy": {},
                           "wasDerivedFrom": {}, "wasAssociatedWith": {},
                           "wasInvalidatedBy": {}, "wasInformedBy": {}}
    counters: dict[str, int] = {}

    def rel(kind: str, body: dict[str, Any]) -> None:
        counters[kind] = counters.get(kind, 0) + 1
        doc[kind][f"_:{kind}{counters[kind]}"] = body

    for eid, ent in project.entities().items():
        attrs: dict[str, Any] = {"prov:type": qname("air", ent.kind), "prov:label": ent.name}
        for key, value in (("air:digest", ent.digest), ("air:version", ent.version),
                           ("air:uri", ent.uri), ("air:size_bytes", ent.size_bytes)):
            if value is not None:
                attrs[key] = value
        if ent.physical:
            attrs["air:physical"] = True
        doc["entity"][qname("ent", eid)] = attrs

    for act in project.activities():
        aid = qname("act", act.activity_id)
        attrs = {"prov:type": qname("air", act.kind.value), "prov:label": act.title,
                 "air:status": act.status.value}
        for key, ts in (("prov:startTime", act.started_at), ("prov:endTime", act.ended_at)):
            t = _time(ts)
            if t:
                attrs[key] = t
        if act.trace_id:
            attrs["air:trace_id"] = act.trace_id
        if act.requirements:
            attrs["air:requirements"] = " ".join(act.requirements)
        doc["activity"][aid] = attrs
        for actor in act.actors:
            agent = qname("agt", actor)
            doc["agent"].setdefault(agent, {"prov:label": actor})
            rel("wasAssociatedWith", {"prov:activity": aid, "prov:agent": agent})
        if act.parent_id:
            rel("wasInformedBy", {"prov:informed": aid,
                                  "prov:informant": qname("act", act.parent_id)})

    for e in project.ledger.query(kind="activity.used"):
        body: dict[str, Any] = {"prov:activity": qname("act", e.data["activity_id"]),
                                "prov:entity": qname("ent", e.data["entity_id"])}
        t = _time(e.ts)
        if t:
            body["prov:time"] = t
        rel("used", body)
    for e in project.ledger.query(kind="activity.generated"):
        aid = qname("act", e.data["activity_id"])
        out = qname("ent", e.data["entity_id"])
        body = {"prov:entity": out, "prov:activity": aid}
        t = _time(e.ts)
        if t:
            body["prov:time"] = t
        rel("wasGeneratedBy", body)
        for src in e.data.get("derived_from", []):
            rel("wasDerivedFrom", {"prov:generatedEntity": out,
                                   "prov:usedEntity": qname("ent", src),
                                   "prov:activity": aid})
    for e in project.ledger.query(kind="entity.replaced"):
        body = {"prov:entity": qname("ent", e.subject),
                "prov:activity": qname("act", e.data["activity_id"])}
        t = _time(e.ts)
        if t:
            body["prov:time"] = t
        rel("wasInvalidatedBy", body)
    return {k: v for k, v in doc.items() if v}
