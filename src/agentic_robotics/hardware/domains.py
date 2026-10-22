"""Domain packs: the engineering knowledge of each of AIR's 19 domains (D01 to D19).

A pack holds a domain's vocabulary, standards, invariants, failure taxonomy,
recurring checks and machine-checkable manifest rules. The same pack is used two ways:

- **Validation.** ``DomainLibrary.check(manifest, catalog)`` applies the rules, for
  example a lithium pack usually has a BMS, or a safety scanner is wired to a safety
  controller. Rules are engineering *advice*: findings are warnings unless the caller
  asks for ``strict``, because AIR never requires particular hardware.
- **Briefing.** ``DomainLibrary.brief(domains)`` renders what an agent working in those
  domains must know and respect.

Rules are data with a small, closed set of kinds, so a pack can never execute code:

``requires_component``
    when a trigger component is present, the robot must also have one matching ``any_of``
``requires_relation``
    each trigger component must be related to a component matching ``any_of``, by
    ``relation`` (or ``any``), including parent/child
``requires_property``
    each trigger component must state ``property``
``requires_interface``
    each trigger component must declare an interface whose kind is in ``any_of``
``max_count``
    at most ``max`` trigger components
"""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from ..contracts.manifest import ComponentManifest, ComponentSpec
from ..core.errors import SchemaError
from ..core.schema import Contract, contract
from .catalog import DOMAINS, DeviceCatalog, matches
from .validate import Finding, Level

__requirements__ = ("REQ03", "D14", "D15")

DOMAINS_FILE = Path(__file__).with_name("data") / "domains" / "domains.json"


class RuleKind(StrEnum):
    REQUIRES_COMPONENT = "requires_component"
    REQUIRES_RELATION = "requires_relation"
    REQUIRES_PROPERTY = "requires_property"
    REQUIRES_INTERFACE = "requires_interface"
    MAX_COUNT = "max_count"


@dataclass(frozen=True)
class Term:
    term: str
    definition: str


@dataclass(frozen=True)
class FailureClass:
    failure_id: str
    name: str
    description: str = ""
    detect_via: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainCheck:
    check_id: str
    description: str
    basis: str = "condition"
    interval: float | None = None


@dataclass(frozen=True)
class DomainRule:
    rule_id: str
    kind: RuleKind
    when: tuple[str, ...]
    message: str
    level: Level = Level.WARNING
    any_of: tuple[str, ...] = ()
    relation: str = "any"
    property: str | None = None
    max: int | None = None
    rationale: str = ""
    standards: tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainPack:
    domain_id: str
    name: str
    summary: str = ""
    scope: tuple[str, ...] = ()
    vocabulary: tuple[Term, ...] = ()
    standards: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    failure_taxonomy: tuple[FailureClass, ...] = ()
    checks: tuple[DomainCheck, ...] = ()
    rules: tuple[DomainRule, ...] = ()


@contract("domain_library", version="1.0")
class DomainLibrary(Contract):
    domains: tuple[DomainPack, ...] = ()

    def __post_init__(self) -> None:
        ids = [d.domain_id for d in self.domains]
        dupes = sorted(i for i, n in Counter(ids).items() if n > 1)
        if dupes:
            raise SchemaError("duplicate domain packs", domains=dupes)

    def get(self, domain_id: str) -> DomainPack | None:
        return next((d for d in self.domains if d.domain_id == domain_id), None)

    def for_types(self, type_ids: Iterable[str], catalog: DeviceCatalog) -> list[DomainPack]:
        """Packs relevant to some device types: their declared domains plus pack scopes."""
        wanted: set[str] = set()
        for tid in type_ids:
            t = catalog.type(tid)
            if t is not None:
                wanted.update(t.domains)
            wanted.update(d.domain_id for d in self.domains
                          if any(matches(tid, s) for s in d.scope))
        return [d for d in self.domains if d.domain_id in wanted]

    # -- validation -----------------------------------------------------------------------
    def check(self, manifest: ComponentManifest, catalog: DeviceCatalog, *,
              strict: bool = False) -> list[Finding]:
        """Rule findings: warnings by default; with ``strict`` each rule's own level."""
        out: list[Finding] = []
        for pack in self.domains:
            for rule in pack.rules:
                for f in _apply(rule, manifest):
                    out.append(f if strict else dataclasses.replace(f, level=Level.WARNING))
        return out

    def problems(self, catalog: DeviceCatalog) -> list[str]:
        """Rule and pack consistency against a catalog (every pattern must match)."""
        out: list[str] = []
        seen: Counter[str] = Counter()

        def known(pattern: str) -> bool:
            return bool(catalog.match(pattern) or catalog.match_classes(pattern)
                        or catalog.protocol(pattern)
                        or any(matches(p.protocol_id, pattern) for p in catalog.protocols))

        for d in self.domains:
            if d.domain_id not in DOMAINS:
                out.append(f"{d.domain_id}: unknown domain")
            for pattern in d.scope:
                if not known(pattern):
                    out.append(f"{d.domain_id}: scope {pattern} matches nothing")
            for ident in ([r.rule_id for r in d.rules] + [f.failure_id for f in d.failure_taxonomy]
                          + [c.check_id for c in d.checks]):
                seen[ident] += 1
                if not ident.startswith(d.domain_id + "."):
                    out.append(f"{ident}: id must start with {d.domain_id}.")
            for r in d.rules:
                for pattern in (*r.when, *r.any_of):
                    if not known(pattern):
                        out.append(f"{r.rule_id}: {pattern} matches nothing")
                if r.kind in (RuleKind.REQUIRES_COMPONENT, RuleKind.REQUIRES_RELATION,
                              RuleKind.REQUIRES_INTERFACE) and not r.any_of:
                    out.append(f"{r.rule_id}: {r.kind.value} needs any_of")
                if r.kind is RuleKind.REQUIRES_PROPERTY and not r.property:
                    out.append(f"{r.rule_id}: requires_property needs property")
                if r.kind is RuleKind.MAX_COUNT and r.max is None:
                    out.append(f"{r.rule_id}: max_count needs max")
        out.extend(f"{i}: duplicate id" for i, n in seen.items() if n > 1)
        return out

    # -- briefing -------------------------------------------------------------------------
    def brief(self, domain_ids: Iterable[str], *, rules: bool = True,
              failures: bool = True) -> str:
        """What an agent working in these domains must know and respect (Markdown)."""
        lines: list[str] = []
        for did in dict.fromkeys(domain_ids):
            d = self.get(did)
            if d is None:
                continue
            lines += [f"## {d.domain_id} {d.name}", d.summary, ""]
            if d.invariants:
                lines += ["Invariants (must hold):", *(f"- {i}" for i in d.invariants), ""]
            if failures and d.failure_taxonomy:
                lines += ["Failure classes:",
                          *(f"- {f.failure_id} {f.name}: {f.description}"
                            + (f" (detect via {', '.join(f.detect_via)})" if f.detect_via
                               else "") for f in d.failure_taxonomy), ""]
            if rules and d.rules:
                lines += ["Checked rules:", *(f"- {r.rule_id} [{r.level.value}] {r.message}"
                                              for r in d.rules), ""]
            if d.standards:
                lines += [f"Standards: {', '.join(d.standards)}", ""]
        return "\n".join(lines).strip()


def _matching(manifest: ComponentManifest, patterns: tuple[str, ...]) -> list[ComponentSpec]:
    return [c for c in manifest.components
            if c.type_id and any(matches(c.type_id, p) for p in patterns)]


def _related(manifest: ComponentManifest, c: ComponentSpec, relation: str
             ) -> list[ComponentSpec]:
    ids = [other for kind, other in manifest.neighbours(c.component_id)
           if relation == "any" or kind in (relation, "part_of", "has_part")]
    return [x for x in manifest.components if x.component_id in ids]


def _apply(rule: DomainRule, manifest: ComponentManifest) -> list[Finding]:
    triggers = _matching(manifest, rule.when)
    if not triggers:
        return []
    note = f" [{rule.rule_id}]"
    if rule.kind is RuleKind.REQUIRES_COMPONENT:
        if _matching(manifest, rule.any_of):
            return []
        return [Finding(rule.level, manifest.robot_id, rule.message + note)]
    if rule.kind is RuleKind.MAX_COUNT:
        if rule.max is not None and len(triggers) > rule.max:
            return [Finding(rule.level, manifest.robot_id,
                            f"{rule.message} ({len(triggers)} > {rule.max}){note}")]
        return []
    out = []
    for c in triggers:
        if rule.kind is RuleKind.REQUIRES_RELATION:
            ok = any(x.type_id and any(matches(x.type_id, p) for p in rule.any_of)
                     for x in _related(manifest, c, rule.relation))
        elif rule.kind is RuleKind.REQUIRES_PROPERTY:
            ok = rule.property in c.properties
        else:  # requires_interface
            ok = any(i.kind in rule.any_of or any(matches(i.kind, p) for p in rule.any_of)
                     for i in c.interfaces)
        if not ok:
            out.append(Finding(rule.level, c.component_id, rule.message + note))
    return out


_LIBRARY: DomainLibrary | None = None


def builtin_domains() -> DomainLibrary:
    """The domain packs that ship with AIR (cached; empty if none are installed)."""
    global _LIBRARY
    if _LIBRARY is None:
        if DOMAINS_FILE.exists():
            doc = json.loads(DOMAINS_FILE.read_text("utf-8"))
            _LIBRARY = DomainLibrary.from_dict({"$schema": "ar.domain_library/1.0", **doc})
        else:
            _LIBRARY = DomainLibrary()
    return _LIBRARY
