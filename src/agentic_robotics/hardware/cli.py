"""``air hardware``: browse the catalog, check manifests, and build task context."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from ..contracts.manifest import ComponentManifest, DocumentRef, Sensitivity
from .catalog import DeviceCatalog, project_catalog
from .domains import builtin_domains
from .validate import Level, check_manifest, maintenance_plan, telemetry_limits

__requirements__ = ("REQ03", "REQ16")


def _catalog(args: argparse.Namespace) -> DeviceCatalog:
    return project_catalog(args.project)


def cmd_types(args: argparse.Namespace) -> int:
    safety = True if args.safety else None
    rows = _catalog(args).search(args.text or "", category=args.category, domain=args.domain,
                                 interface=args.interface, provides=args.provides,
                                 safety=safety)
    for t in rows:
        flag = " [safety]" if t.safety_relevant else ""
        print(f"{t.type_id:<46} {t.name}{flag}")
    print(f"{len(rows)} device type(s)", file=sys.stderr)
    return 0


def cmd_classes(args: argparse.Namespace) -> int:
    rows = [c for c in _catalog(args).robot_classes
            if not args.text or args.text.lower() in f"{c.class_id} {c.name}".lower()]
    for c in sorted(rows, key=lambda c: c.class_id):
        print(f"{c.class_id:<46} {c.name}  ({len(c.slots)} slots)")
    print(f"{len(rows)} robot class(es)", file=sys.stderr)
    return 0


def cmd_protocols(args: argparse.Namespace) -> int:
    rows = [p for p in _catalog(args).protocols if not args.layer or p.layer == args.layer]
    for p in sorted(rows, key=lambda p: p.protocol_id):
        print(f"{p.protocol_id:<32} {p.layer:<12} {p.realtime:<5} {p.name}")
    print(f"{len(rows)} protocol(s)", file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    cat = _catalog(args)
    item: Any = cat.type(args.id) or cat.robot_class(args.id) or cat.protocol(args.id)
    if item is None:
        print(f"unknown id {args.id}", file=sys.stderr)
        return 2
    from ..core.schema import _dump
    print(json.dumps(_dump(item), indent=2, ensure_ascii=False))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cat = _catalog(args)
    worst = 0
    for path in args.manifests:
        manifest = ComponentManifest.load(path)
        findings = check_manifest(manifest, cat, strict=args.strict, root=args.project,
                                  domains=None if args.no_domains else builtin_domains())
        errors = [f for f in findings if f.level is Level.ERROR]
        print(f"{path}: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)")
        for f in findings:
            print(f"  {f}")
        worst = max(worst, 1 if errors else 0)
    return worst


def cmd_plan(args: argparse.Namespace) -> int:
    cat = _catalog(args)
    manifest = ComponentManifest.load(args.manifest)
    print(f"# Monitoring ({manifest.robot_id})")
    for lim in telemetry_limits(manifest, cat):
        s = lim.signal
        bounds = ", ".join(f"{k} {v:g}" for k, v in (
            ("warn above", s.warn_above), ("critical above", s.critical_above),
            ("warn below", s.warn_below), ("critical below", s.critical_below))
            if v is not None)
        print(f"  {lim.component_id:<24} {s.name:<28} {s.unit or '':<8} {bounds}")
    print(f"# Maintenance ({manifest.robot_id})")
    for m in maintenance_plan(manifest, cat):
        every = f"every {m.item.interval:g} {m.item.basis.value}" if m.item.interval \
            else m.item.basis.value
        flag = " [safety]" if m.safety_relevant else ""
        print(f"  {m.component_id:<24} {m.item.item:<28} {every:<24} {m.item.action}{flag}")
    return 0


def cmd_template(args: argparse.Namespace) -> int:
    """A manifest skeleton with one component per required slot of a robot class."""
    cat = _catalog(args)
    rc = cat.robot_class(args.robot_class)
    if rc is None:
        print(f"unknown robot class {args.robot_class}", file=sys.stderr)
        return 2
    components: list[dict[str, Any]] = []
    for slot in rc.slots:
        candidates = [t for p in slot.types for t in cat.match(p)]
        if not candidates:
            continue
        t = candidates[0]
        for n in range(max(slot.min, 1 if args.all_slots else 0)):
            components.append({
                "component_id": f"{slot.slot}-{n + 1}" if slot.min > 1 else slot.slot,
                "kind": t.name, "revision": "r1", "type_id": t.type_id,
                "properties": {p.name: None for p in t.properties if p.required}})
    doc = {"$schema": "ar.component_manifest/1.1", "robot_id": args.robot_id,
           "name": f"{rc.name} ({args.robot_id})", "hardware_revision": "r1",
           "robot_class": rc.class_id, "adapter": {"kind": "fake"},
           "nominal_capabilities": list(rc.capabilities), "components": components}
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    print("fill in the null properties, then: air hardware check <file>", file=sys.stderr)
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """The hardware facts a task needs: ranked, budgeted, with sources."""
    from .context import build_context

    project = None
    if (Path(args.project) / ".air").is_dir():
        from ..provenance.project import Project
        project = Project.open(args.project)
    observations: dict[str, Any] = {}
    for item in args.observe or []:
        name, _, raw = item.partition("=")
        value, _, unit = raw.partition(" ")
        observations[name] = (float(value), unit or None)
    ctx = build_context(ComponentManifest.load(args.manifest), _catalog(args),
                        focus=args.focus or (), text=args.text or "",
                        observations=observations, project=project,
                        domains=builtin_domains(), budget_chars=args.budget,
                        allow_restricted=not args.no_restricted)
    print(json.dumps(ctx.to_dict(), indent=2) if args.json else ctx.render())
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    """Pin a datasheet or other document to a component (digest checked by ``check``)."""
    from ..provenance.project import file_digest

    root = Path(args.project)
    doc_path = Path(args.file)
    rel = doc_path.resolve().relative_to(root.resolve()).as_posix()
    manifest = ComponentManifest.load(args.manifest)
    component = manifest.component(args.component)
    if component is None:
        print(f"unknown component {args.component}", file=sys.stderr)
        return 2
    ref = DocumentRef(path=rel, digest=file_digest(doc_path), kind=args.kind,
                      title=args.title or doc_path.name,
                      sensitivity=Sensitivity(args.sensitivity))
    docs = (*(d for d in component.documents if d.path != rel), ref)
    updated = dataclasses.replace(component, documents=docs)
    manifest = manifest.evolve(components=tuple(
        updated if c.component_id == component.component_id else c
        for c in manifest.components))
    manifest.save(args.manifest)
    if (root / ".air").is_dir():
        from ..provenance.project import Project
        Project.open(root).entity(doc_path, kind=f"document.{args.kind}",
                                  attributes={"robot_id": manifest.robot_id,
                                              "component": component.component_id,
                                              "sensitivity": ref.sensitivity.value})
    print(f"attached {rel} ({ref.digest[:19]}...) to {component.component_id}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from .semantics import manifest_graph, thing_model

    cat = _catalog(args)
    t = cat.type(args.target)
    if t is not None:
        doc = thing_model(t)
    elif Path(args.target).is_file():
        doc = manifest_graph(ComponentManifest.load(args.target), cat)
    else:
        print(f"{args.target} is neither a device type nor a manifest file", file=sys.stderr)
        return 2
    print(json.dumps(doc, indent=2, ensure_ascii=False))
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    lib = builtin_domains()
    ids = args.domains or [d.domain_id for d in lib.domains]
    text = lib.brief(ids)
    if not text:
        print("no domain packs found for " + ", ".join(ids), file=sys.stderr)
        return 2
    print(text)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cat = _catalog(args)
    st = cat.stats()
    problems = cat.problems()
    lib = builtin_domains()
    problems += lib.problems(cat)
    st["domain_packs"] = len(lib.domains)
    st["domain_rules"] = sum(len(d.rules) for d in lib.domains)
    st["problems"] = len(problems)
    print(json.dumps(st, indent=2))
    for p in problems[: args.show]:
        print(f"problem: {p}", file=sys.stderr)
    return 1 if problems and args.strict else 0


def add_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    h = sub.add_parser("hardware", help="hardware catalog: robot classes, device types, "
                                        "protocols; manifest checks")
    hs = h.add_subparsers(dest="action", required=True)

    def proj(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--project", default=".",
                       help="project whose .air/hardware/*.json extend the catalog")
        return p

    t = proj(hs.add_parser("types", help="search device types"))
    t.add_argument("text", nargs="?")
    t.add_argument("--category")
    t.add_argument("--domain", help="D01..D19")
    t.add_argument("--interface", help="protocol id, e.g. can.canopen")
    t.add_argument("--provides", help="capability, e.g. torque")
    t.add_argument("--safety", action="store_true", help="only safety-relevant types")
    t.set_defaults(fn=cmd_types)
    c = proj(hs.add_parser("classes", help="list robot classes"))
    c.add_argument("text", nargs="?")
    c.set_defaults(fn=cmd_classes)
    p = proj(hs.add_parser("protocols", help="list protocols and interfaces"))
    p.add_argument("--layer")
    p.set_defaults(fn=cmd_protocols)
    s = proj(hs.add_parser("show", help="show a device type, robot class or protocol"))
    s.add_argument("id")
    s.set_defaults(fn=cmd_show)
    k = proj(hs.add_parser("check", help="check manifests against the catalog"))
    k.add_argument("manifests", nargs="+")
    k.add_argument("--strict", action="store_true",
                   help="treat composition advice (slots, domain rules, untyped parts) as "
                        "errors; AIR never requires particular hardware otherwise")
    k.add_argument("--no-domains", action="store_true", help="skip domain-pack rules")
    k.set_defaults(fn=cmd_check)
    cx = proj(hs.add_parser("context", help="hardware facts for a task, ranked and budgeted"))
    cx.add_argument("manifest")
    cx.add_argument("--focus", action="append", help="component id (repeatable)")
    cx.add_argument("--text", help='free-text focus, e.g. "left wheel overheating"')
    cx.add_argument("--observe", action="append",
                    help='reading "component.signal=VALUE [UNIT]" (repeatable)')
    cx.add_argument("--budget", type=int, default=4000, help="characters")
    cx.add_argument("--no-restricted", action="store_true",
                    help="withhold restricted facts (for models where data leaves the device)")
    cx.add_argument("--json", action="store_true")
    cx.set_defaults(fn=cmd_context)
    at = proj(hs.add_parser("attach", help="pin a document to a component by digest"))
    at.add_argument("manifest")
    at.add_argument("component")
    at.add_argument("file")
    at.add_argument("--kind", default="datasheet")
    at.add_argument("--title")
    at.add_argument("--sensitivity", default="internal", choices=[s.value for s in Sensitivity])
    at.set_defaults(fn=cmd_attach)
    ex = proj(hs.add_parser("export", help="WoT Thing Model (device type) or JSON-LD (manifest)"))
    ex.add_argument("target", help="device type id or manifest path")
    ex.set_defaults(fn=cmd_export)
    br = proj(hs.add_parser("brief", help="domain briefing for agents and people"))
    br.add_argument("domains", nargs="*", help="D01..D19 (default: all)")
    br.set_defaults(fn=cmd_brief)
    pl = proj(hs.add_parser("plan", help="monitoring limits and maintenance for a manifest"))
    pl.add_argument("manifest")
    pl.set_defaults(fn=cmd_plan)
    tp = proj(hs.add_parser("template", help="manifest skeleton for a robot class"))
    tp.add_argument("robot_class")
    tp.add_argument("--robot-id", default="robot-1")
    tp.add_argument("--all-slots", action="store_true",
                    help="include optional slots (one component each)")
    tp.set_defaults(fn=cmd_template)
    st = proj(hs.add_parser("stats", help="catalog size and consistency"))
    st.add_argument("--strict", action="store_true")
    st.add_argument("--show", type=int, default=20, help="problems to print")
    st.set_defaults(fn=cmd_stats)
