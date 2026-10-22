"""Static checks for a dora dataflow before it runs (in the spirit of ``dora validate``).

The structure checked is the one in ``examples/python-operator-dataflow/dataflow.yml``:
``nodes: [{id, path | operator | operators, inputs: {name: "<node>/<output>" |
"dora/timer/<millis|secs>/<n>"}, outputs: [..]}]``. dora's own ``validate`` also checks
type annotations; this does not. AIR skill-operator nodes (named in ``air_nodes``) must
wire the ports :mod:`.operator` defines.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from ...core.errors import SchemaError, UnsupportedOperation
from .operator import INPUTS, OUTPUTS

__requirements__ = ("REQ18",)

_TIMER = re.compile(r"^dora/timer/(millis|secs)/[1-9][0-9]*$")
_RUNNABLE = ("path", "operator", "operators", "custom")


def load_dataflow(path: str | Path) -> dict[str, Any]:
    text = Path(path).read_text("utf-8")
    if str(path).endswith(".json"):
        doc = json.loads(text)
    else:
        try:
            import yaml  # type: ignore[import-not-found,import-untyped,unused-ignore]
        except ImportError as exc:
            raise UnsupportedOperation("PyYAML is needed to read a YAML dataflow; or pass "
                                       "JSON", path=str(path)) from exc
        doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise SchemaError("dataflow must be a mapping", path=str(path))
    return doc


def _outputs(node: Mapping[str, Any]) -> list[str]:
    outs = list(node.get("outputs") or [])
    op = node.get("operator")
    if isinstance(op, Mapping):
        outs += list(op.get("outputs") or [])
    for sub in node.get("operators") or []:
        outs += list(sub.get("outputs") or [])
    return [str(o) for o in outs]


def _inputs(node: Mapping[str, Any]) -> dict[str, Any]:
    ins = dict(node.get("inputs") or {})
    op = node.get("operator")
    if isinstance(op, Mapping):
        ins.update(op.get("inputs") or {})
    for sub in node.get("operators") or []:
        ins.update(sub.get("inputs") or {})
    return ins


def validate_dataflow(doc: Mapping[str, Any], *, air_nodes: Iterable[str] = ()) -> list[str]:
    problems: list[str] = []
    nodes = doc.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return ["dataflow has no 'nodes' list"]
    outputs: dict[str, set[str]] = {}
    for i, node in enumerate(nodes):
        if not isinstance(node, Mapping) or not isinstance(node.get("id"), str):
            problems.append(f"node #{i} has no string id")
            continue
        nid = node["id"]
        if nid in outputs:
            problems.append(f"duplicate node id {nid!r}")
        if "/" in nid:
            problems.append(f"node id {nid!r} must not contain '/'")
        if not any(k in node for k in _RUNNABLE):
            problems.append(f"{nid}: needs one of {list(_RUNNABLE)}")
        outs = _outputs(node)
        if len(set(outs)) != len(outs):
            problems.append(f"{nid}: duplicate outputs")
        outputs[nid] = set(outs)
    for node in nodes:
        if not isinstance(node, Mapping) or not isinstance(node.get("id"), str):
            continue
        nid = node["id"]
        for name, src in _inputs(node).items():
            source = src.get("source") if isinstance(src, Mapping) else src
            if not isinstance(source, str):
                problems.append(f"{nid}.{name}: input source must be a string")
                continue
            if source.startswith("dora/"):
                if not _TIMER.match(source):
                    problems.append(f"{nid}.{name}: unknown dora source {source!r}")
                continue
            other, _, out = source.partition("/")
            if other not in outputs:
                problems.append(f"{nid}.{name}: unknown node {other!r}")
            elif out not in outputs[other]:
                problems.append(f"{nid}.{name}: node {other!r} has no output {out!r}")
    by_id: dict[str, Mapping[str, Any]] = {}
    for n in nodes:
        if isinstance(n, Mapping) and isinstance(n.get("id"), str):
            by_id.setdefault(n["id"], n)
    for air in air_nodes:
        node = by_id.get(air)
        if node is None:
            problems.append(f"AIR operator node {air!r} is not in the dataflow")
            continue
        ins = _inputs(node)
        for port, required in INPUTS.items():
            if required and port not in ins:
                problems.append(f"{air}: AIR operator needs input {port!r}")
        extra = set(ins) - set(INPUTS)
        if extra:
            problems.append(f"{air}: unknown AIR operator inputs {sorted(extra)}")
        missing = set(OUTPUTS) - outputs.get(air, set())
        if missing:
            problems.append(f"{air}: AIR operator must declare outputs {sorted(missing)}")
    return problems


__all__ = ["load_dataflow", "validate_dataflow"]
