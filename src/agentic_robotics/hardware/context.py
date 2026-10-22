"""Task context: the hardware facts a specific task needs, ranked and within a budget.

Given a robot manifest and a focus (component ids, free text such as "left wheel
overheating", signals), the selector:

1. seeds components matching the focus (ids, names, types, aliases, signals, failure
   modes; a few synonyms such as *hot* → temperature)
2. walks the component graph (parent/child and typed relations) a few hops out
3. gathers facts for the selected components:
   - what each component is, and its stated properties
   - its effective limits and current readings
   - likely failure modes and maintenance items
   - attached documents
   - ledger history (replacements, invalidated calibration, maintenance)
   - open triage items
   - manifest findings, and the invariants of the domains involved
4. ranks the facts and cuts them to a character budget

Every fact carries its source (catalog entry, manifest, ledger entry, observation), its
age where known, and its sensitivity. With ``allow_restricted=False``, restricted facts
(vendor NDA data, restricted documents) are withheld and counted, never sent.

The *fingerprint* covers the inputs that change rarely: manifest, catalog, focus and
ledger head. Static facts can be reused until it changes (REQ08). Readings are always
fresh.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..contracts.manifest import ComponentManifest, ComponentSpec, Sensitivity
from .catalog import DeviceCatalog, DeviceType, most_sensitive
from .semantics import bind_observation
from .validate import Level, check_manifest, telemetry_limits

if TYPE_CHECKING:
    from ..provenance.project import Project
    from .domains import DomainLibrary

__requirements__ = ("REQ08", "REQ14", "REQ16", "AIR01")

#: Words people use for a symptom -> words that appear in signal and failure names.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "hot": ("temperature", "thermal", "overheat"), "heat": ("temperature", "thermal"),
    "overheat": ("temperature", "thermal"), "overheating": ("temperature", "thermal"),
    "thermal": ("temperature",), "warm": ("temperature",),
    "drain": ("state_of_charge", "current", "battery"), "charge": ("state_of_charge", "charg"),
    "battery": ("battery", "cell", "state_of_charge"), "shake": ("vibration",),
    "vibrating": ("vibration",), "vibrates": ("vibration",), "noise": ("vibration", "noise"),
    "stall": ("current", "torque"), "slip": ("encoder", "odometry", "wheel"),
    "drift": ("drift", "bias", "calibration"), "disconnect": ("packet_loss", "link", "bus"),
    "lag": ("latency", "update_rate"), "blind": ("contamination", "window", "occlusion"),
    "dirty": ("contamination", "window"), "leak": ("leak", "pressure", "vacuum"),
}
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Fact:
    #: component, limit, reading, failure_modes, maintenance, document, history, triage,
    #: finding or invariant
    kind: str
    subject: str         # component id, or domain id for invariants
    text: str
    source: str
    score: float
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    age_s: float | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HardwareContext:
    robot_id: str
    focus: tuple[str, ...]
    components: tuple[str, ...]
    facts: tuple[Fact, ...]
    fingerprint: str
    omitted: int = 0                 # facts cut by the budget
    withheld_restricted: int = 0     # facts not sent because they are restricted
    budget_chars: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"robot_id": self.robot_id, "focus": list(self.focus),
                "components": list(self.components), "fingerprint": self.fingerprint,
                "omitted": self.omitted, "withheld_restricted": self.withheld_restricted,
                "facts": [{"kind": f.kind, "subject": f.subject, "text": f.text,
                           "source": f.source, "sensitivity": f.sensitivity.value,
                           **({"age_s": round(f.age_s, 1)} if f.age_s is not None else {})}
                          for f in self.facts]}

    def render(self) -> str:
        """Compact Markdown for a model or a person, grouped by component."""
        lines = [f"# Hardware context: {self.robot_id}"
                 + (f" (focus: {', '.join(self.focus)})" if self.focus else "")]
        by_subject: dict[str, list[Fact]] = {}
        for f in self.facts:
            by_subject.setdefault(f.subject, []).append(f)
        for subject, facts in by_subject.items():
            lines.append(f"\n## {subject}")
            for f in facts:
                age = f" ({_age(f.age_s)} ago)" if f.age_s is not None else ""
                lines.append(f"- [{f.kind}] {f.text}{age}  <{f.source}>")
        notes = []
        if self.omitted:
            notes.append(f"{self.omitted} lower-ranked facts omitted (budget)")
        if self.withheld_restricted:
            notes.append(f"{self.withheld_restricted} restricted facts withheld")
        if notes:
            lines.append("\n_" + "; ".join(notes) + "_")
        return "\n".join(lines)


def _age(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    for unit, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f} {unit}"
    return f"{seconds:.0f} s"


def _words(text: str) -> set[str]:
    words = set(_WORD.findall(text.lower()))
    for w in list(words):
        words.update(SYNONYMS.get(w, ()))
    return words


def _identity(c: ComponentSpec, t: DeviceType | None) -> set[str]:
    """Words naming what the component *is* (id, kind, vendor, model, type, aliases)."""
    parts = [c.component_id, c.kind, c.vendor or "", c.model or ""]
    if t is not None:
        parts += [t.type_id, t.name, *t.aliases]
    return set(_WORD.findall(" ".join(parts).lower().replace("_", " ")))


def _detail(t: DeviceType | None) -> str:
    """Signal and failure text: weak evidence, since many parts share a temperature."""
    if t is None:
        return ""
    return " ".join([*(s.name for s in t.telemetry), *t.failure_modes]).lower()


def _seed(manifest: ComponentManifest, catalog: DeviceCatalog, focus: Iterable[str],
          text: str) -> dict[str, float]:
    scores: dict[str, float] = {}
    ids = {c.component_id for c in manifest.components}
    for f in focus:
        if f in ids:
            scores[f] = 1.0
    words = {w for w in _words(text) if len(w) > 2} if text else set()
    if not words:
        return scores
    rated = []
    for c in manifest.components:
        t = catalog.type(c.type_id) if c.type_id else None
        ident = len(words & _identity(c, t))
        detail = sum(1 for w in words if w in _detail(t))
        rated.append((c.component_id, ident, detail))
    named = any(ident for _, ident, _ in rated)
    for cid, ident, detail in rated:
        if named and not ident:
            continue       # the text names parts; signal-only matches are not seeds
        score = min(1.0, 0.35 * ident + 0.1 * detail)
        if score > 0:
            scores[cid] = max(scores.get(cid, 0.0), score)
    return scores


def _expand(manifest: ComponentManifest, seeds: dict[str, float], depth: int
            ) -> dict[str, float]:
    scores = dict(seeds)
    frontier = list(seeds)
    for hop in range(1, depth + 1):
        nxt = []
        for cid in frontier:
            for _, other in manifest.neighbours(cid):
                s = seeds.get(cid, scores[cid]) / (1 + hop)
                if s > scores.get(other, 0.0):
                    scores[other] = s
                    nxt.append(other)
        frontier = nxt
    return scores


def build_context(manifest: ComponentManifest, catalog: DeviceCatalog, *,
                  focus: Iterable[str] = (), text: str = "",
                  observations: Mapping[str, Any] | None = None,
                  project: Project | None = None, domains: DomainLibrary | None = None,
                  budget_chars: int = 4000, allow_restricted: bool = True, depth: int = 2,
                  now: float | None = None) -> HardwareContext:
    """Select, rank and budget the hardware facts for a task.

    ``observations`` maps ``<component_id>.<signal>`` to a value, a ``(value, unit)``
    pair, or ``(value, unit, observed_at)``.
    """
    now = time.time() if now is None else now
    focus = tuple(focus)
    seeds = _seed(manifest, catalog, focus, text)
    if not seeds:   # no focus: the whole robot, safety-relevant parts first
        seeds = {c.component_id: 0.3 for c in manifest.components}
    scores = _expand(manifest, seeds, depth)
    words = _words(text)
    facts: list[Fact] = []
    for c in manifest.components:
        if c.component_id not in scores:
            continue
        t = catalog.type(c.type_id) if c.type_id else None
        s = scores[c.component_id] + (0.05 if t and t.safety_relevant else 0.0)
        facts.extend(_component_facts(c, t, s, words))
    facts.extend(_limit_facts(manifest, catalog, scores, words))
    facts.extend(_reading_facts(manifest, catalog, scores, observations or {}, now))
    facts.extend(_finding_facts(manifest, catalog, scores, domains, project))
    if project is not None:
        facts.extend(_history_facts(project, manifest, scores, now))
        facts.extend(_triage_facts(project, manifest, scores, now))
    if domains is not None:
        type_ids = [c.type_id for c in manifest.components
                    if c.type_id and c.component_id in scores]
        for pack in domains.for_types(type_ids, catalog):
            for inv in pack.invariants:
                facts.append(Fact("invariant", f"{pack.domain_id} {pack.name}", inv,
                                  f"domain:{pack.domain_id}", 0.55, Sensitivity.PUBLIC))
    withheld = 0
    if not allow_restricted:
        kept = [f for f in facts if f.sensitivity is not Sensitivity.RESTRICTED]
        withheld = len(facts) - len(kept)
        facts = kept
    facts.sort(key=lambda f: -f.score)
    chosen, used, omitted = [], 0, 0
    for f in facts:
        cost = len(f.text) + len(f.source) + 16
        if used + cost > budget_chars:
            omitted += 1
            continue
        chosen.append(f)
        used += cost
    order = {cid: i for i, cid in enumerate(c.component_id for c in manifest.components)}
    chosen.sort(key=lambda f: (order.get(f.subject, len(order)), -f.score))
    selected = tuple(sorted(scores, key=lambda cid: -scores[cid]))
    return HardwareContext(manifest.robot_id, focus or tuple(sorted(words)), selected,
                           tuple(chosen), _fingerprint(manifest, catalog, focus, text, project),
                           omitted, withheld, budget_chars)


def _fingerprint(manifest: ComponentManifest, catalog: DeviceCatalog, focus: tuple[str, ...],
                 text: str, project: Project | None) -> str:
    h = hashlib.sha256()
    h.update(manifest.to_json().encode())
    h.update(json.dumps([catalog.name, len(catalog.device_types), list(focus), text]).encode())
    if project is not None:
        h.update(project.ledger.head.encode())
    return "sha256:" + h.hexdigest()


def _component_facts(c: ComponentSpec, t: DeviceType | None, s: float,
                     words: set[str]) -> list[Fact]:
    sens = most_sensitive(c.sensitivity, t.sensitivity) if t else c.sensitivity
    what = f"{c.kind}" + (f" = {t.name} ({t.type_id})" if t else " (untyped)")
    if c.vendor or c.model:
        what += f", {c.vendor or ''} {c.model or ''}".rstrip()
    if t and t.safety_relevant:
        what += "; SAFETY-RELEVANT"
    if c.properties:
        stated = ", ".join(f"{k}={_value(v)}" for k, v in list(c.properties.items())[:8])
        what += f"; {stated}"
    out = [Fact("component", c.component_id, what, f"manifest:{c.component_id}", s, sens)]
    if t is None:
        return out
    if t.failure_modes:
        boost = 0.2 if any(w in " ".join(t.failure_modes).lower() for w in words) else 0.0
        out.append(Fact("failure_modes", c.component_id, "; ".join(t.failure_modes),
                        f"catalog:{t.type_id}", s * 0.7 + boost, t.sensitivity))
    for m in t.maintenance[:4]:
        every = f"every {m.interval:g} {m.basis.value}" if m.interval else m.basis.value
        out.append(Fact("maintenance", c.component_id, f"{m.item}: {m.action} ({every})",
                        f"catalog:{t.type_id}", s * 0.45, t.sensitivity))
    for d in c.documents:
        out.append(Fact("document", c.component_id,
                        f"{d.kind}: {d.title or d.path} ({d.path}, {d.digest[:19]}…)",
                        f"manifest:{c.component_id}", s * 0.6, d.sensitivity))
    return out


def _value(v: Any) -> str:
    if isinstance(v, dict) and set(v) == {"value", "unit"}:
        return f"{v['value']:g} {v['unit']}" if isinstance(v["value"], int | float) \
            else str(v["value"])
    return str(v)


def _limit_facts(manifest: ComponentManifest, catalog: DeviceCatalog,
                 scores: dict[str, float], words: set[str]) -> list[Fact]:
    out = []
    comps = {c.component_id: c for c in manifest.components}
    for lim in telemetry_limits(manifest, catalog):
        if lim.component_id not in scores:
            continue
        sig = lim.signal
        bounds = ", ".join(f"{k} {v:g}" for k, v in (
            ("warn >", sig.warn_above), ("critical >", sig.critical_above),
            ("warn <", sig.warn_below), ("critical <", sig.critical_below)) if v is not None)
        if not bounds:
            continue
        relevant = any(w in sig.name for w in words if len(w) > 3)
        score = scores[lim.component_id] * (0.85 if relevant else 0.5)
        if lim.source.startswith("manifest:"):
            score += 0.05
        out.append(Fact("limit", lim.component_id,
                        f"{sig.name} [{sig.unit or '-'}]: {bounds}", lim.source, score,
                        comps[lim.component_id].sensitivity))
    return out


def _reading_facts(manifest: ComponentManifest, catalog: DeviceCatalog,
                   scores: dict[str, float], observations: Mapping[str, Any],
                   now: float) -> list[Fact]:
    out = []
    for name, raw in observations.items():
        value, unit, at = raw, None, None
        if isinstance(raw, tuple | list):
            value, unit, at = (*raw, None, None)[:3]
        bound = bind_observation(manifest, catalog, name, float(value), unit)
        if bound is None or bound.component_id not in scores:
            continue
        status = bound.status or bound.problem or "?"
        boost = {"critical": 1.0, "warn": 0.6}.get(bound.status or "", 0.1)
        text = f"{bound.signal.name} = {bound.value:g} {bound.unit or ''} -> {status}"
        if bound.limit is not None and bound.status != "ok":
            text += f" (limit from {bound.limit.source})"
        out.append(Fact("reading", bound.component_id, text, f"observation:{name}",
                        scores[bound.component_id] + boost, Sensitivity.INTERNAL,
                        (now - at) if at is not None else None))
    return out


def _finding_facts(manifest: ComponentManifest, catalog: DeviceCatalog,
                   scores: dict[str, float], domains: DomainLibrary | None,
                   project: Project | None) -> list[Fact]:
    root = project.root if project is not None else None
    out = []
    for f in check_manifest(manifest, catalog, root=root, domains=domains):
        subject = f.where if f.where in scores else None
        if subject is None and f.where.split(".")[0] == manifest.robot_id:
            subject = manifest.robot_id
        if subject is None:
            continue
        base = scores.get(subject, 0.4)
        out.append(Fact("finding", subject, f"{f.level.value}: {f.message}",
                        "air hardware check", base * (0.8 if f.level is Level.ERROR else 0.5),
                        Sensitivity.INTERNAL))
    return out


def _history_facts(project: Project, manifest: ComponentManifest, scores: dict[str, float],
                   now: float) -> list[Fact]:
    out = []
    names = set(scores)
    for e in project.ledger:
        subject = e.subject
        hit = None
        if e.kind in ("entity.replaced", "entity.registered") and subject.startswith(
                "component:"):
            hit = subject.split(":", 1)[1].split("@", 1)[0]
        elif e.kind == "calibration.invalidated":
            hit = subject
        if hit is None or hit not in names:
            continue
        if e.kind == "entity.registered":
            continue
        if e.kind == "entity.replaced":
            installed = str(e.data.get("installed", "?")).split(":", 1)[-1]
            text = f"replaced by {installed}: {e.data.get('reason', '')}".strip()
        else:
            text = f"calibration invalidated: {e.data.get('reason', '')}".strip()
        out.append(Fact("history", hit, text, f"ledger:{e.seq}", scores[hit] * 0.75,
                        Sensitivity.INTERNAL, max(0.0, now - e.ts)))
    return out


def _triage_facts(project: Project, manifest: ComponentManifest, scores: dict[str, float],
                  now: float) -> list[Fact]:
    try:
        from ..triage.book import TriageBook
        items = TriageBook(project).items()
    except Exception:  # triage is optional evidence; never fail context building on it
        return []
    out = []
    for item in items:
        if item.robot_id not in (None, manifest.robot_id):
            continue
        labels = " ".join(item.labels.values())
        hit = next((cid for cid in scores if cid in labels or cid == item.subject), None)
        if hit is None and item.robot_id != manifest.robot_id:
            continue
        subject = hit or manifest.robot_id
        out.append(Fact("triage", subject,
                        f"open {item.severity.value}/{item.priority.value}: {item.title} "
                        f"({item.state.value}, seen {item.count}x)",
                        f"triage:{item.item_id}", scores.get(subject, 0.5) + 0.3,
                        Sensitivity.INTERNAL, max(0.0, now - item.last_seen)))
    return out
