"""JSON Schema (Draft 2020-12) for every contract, generated from the dataclass hints.

The schemas let non-Python tools (ROS C++ nodes, a TypeScript workbench, editors)
validate AIR documents without AIR. They mirror the strict parser: unknown fields are
rejected (``additionalProperties: false``), fields without defaults are ``required``,
enums are closed, and ``$schema`` must name the same contract with the same major
version (short tag or URI form).

Published files live in ``schemas/ar.<name>/<version>.json`` and are never edited once a
version is published. ``air schema check`` fails when the generated schema drifts from
the committed one, or when a minor version is not a backwards-compatible superset of the
previous minor.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
import types
import typing
from enum import Enum
from pathlib import Path
from typing import Any, Union, get_args, get_origin

from .errors import SchemaError
from .schema import SCHEMA_KEY, Contract, _hints, registered_contracts, schema_uri

__requirements__ = ("REQ01", "REQ18", "F02", "F06")

DRAFT = "https://json-schema.org/draft/2020-12/schema"


def contract_schema(cls: type[Contract]) -> dict[str, Any]:
    defs: dict[str, Any] = {}
    root = _object_schema(cls, defs, top_level=True)
    major = cls.VERSION.split(".")[0]
    name = re.escape(f"ar.{cls.SCHEMA}")
    root["properties"] = {
        SCHEMA_KEY: {
            "type": "string",
            "description": "Contract tag (ar.<name>/<major>.<minor>) or its schema URI",
            "pattern": rf"^(?:{name}/{major}\.\d+|https?://.+/{name}/{major}\.\d+\.json)$",
        },
        **root["properties"],
    }
    doc: dict[str, Any] = {
        "$schema": DRAFT,
        "$id": schema_uri(cls.schema_id()),
        "title": cls.schema_id(),
        **root,
    }
    if defs:
        doc["$defs"] = dict(sorted(defs.items()))
    return doc


def _description(cls: type) -> str | None:
    doc = (cls.__doc__ or "").strip()
    if not doc or doc.startswith(cls.__name__ + "("):  # dataclass auto-generated docstring
        return None
    return " ".join(doc.split("\n\n")[0].split())


def _object_schema(cls: type, defs: dict[str, Any], top_level: bool = False) -> dict[str, Any]:
    hints = _hints(cls)
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in dataclasses.fields(cls):
        prop = _type_schema(hints[f.name], defs)
        if f.default is not dataclasses.MISSING:
            default = _json_default(f.default)
            if default is not _NO_DEFAULT:
                prop = {**prop, "default": default}
        elif f.default_factory is dataclasses.MISSING:
            required.append(f.name)
        props[f.name] = prop
    out: dict[str, Any] = {"type": "object"}
    desc = _description(cls)
    if desc:
        out["description"] = desc
    if not top_level and issubclass(cls, Contract):
        # Nested contracts may carry their own $schema tag; the parser ignores it.
        props = {SCHEMA_KEY: {"type": "string"}, **props}
    out["properties"] = props
    if required:
        out["required"] = required
    out["additionalProperties"] = False
    return out


_NO_DEFAULT = object()


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, tuple) and all(isinstance(v, (bool, int, float, str, Enum))
                                        for v in value):
        return [v.value if isinstance(v, Enum) else v for v in value]
    return _NO_DEFAULT


def _type_schema(tp: Any, defs: dict[str, Any]) -> dict[str, Any]:
    if tp is Any:
        return {}
    if tp is type(None):
        return {"type": "null"}
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        options = [_type_schema(a, defs) for a in get_args(tp)]
        return {"anyOf": options}
    if origin is typing.Literal:
        return {"enum": list(get_args(tp))}
    if origin in (list, tuple):
        args = get_args(tp)
        item = args[0] if args else Any
        return {"type": "array", "items": _type_schema(item, defs)}
    if origin is dict or tp is dict:
        args = get_args(tp)
        value_tp = args[1] if len(args) == 2 else Any
        return {"type": "object", "additionalProperties": _type_schema(value_tp, defs)}
    if isinstance(tp, type) and issubclass(tp, Enum):
        return {"enum": [m.value for m in tp]}
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        name = tp.__name__
        if name not in defs:
            defs[name] = {}  # placeholder to stop recursion
            defs[name] = _object_schema(tp, defs)
        return {"$ref": f"#/$defs/{name}"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is str:
        return {"type": "string"}
    raise SchemaError("cannot express type in JSON Schema", type=str(tp))


# -- export and checks ---------------------------------------------------------------------
def schema_path(root: Path, cls: type[Contract]) -> Path:
    return root / f"ar.{cls.SCHEMA}" / f"{cls.VERSION}.json"


def render(doc: dict[str, Any]) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


def export_schemas(root: str | Path) -> list[Path]:
    """Write the current version of every registered contract's schema, plus an index.
    Existing files for *other* versions are left untouched (published = frozen)."""
    root = Path(root)
    written = []
    index = {}
    for name, cls in sorted(registered_contracts().items()):
        path = schema_path(root, cls)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(contract_schema(cls)), encoding="utf-8", newline="\n")
        written.append(path)
        index[f"ar.{name}"] = {"current": cls.VERSION, "uri": schema_uri(cls.schema_id())}
    (root / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8", newline="\n")
    return written


def check_schemas(root: str | Path) -> list[str]:
    """Problems: missing or drifted current schemas, and minor versions that are not
    backwards-compatible supersets of the previous minor within the same major."""
    root = Path(root)
    problems: list[str] = []
    for _, cls in sorted(registered_contracts().items()):
        path = schema_path(root, cls)
        expected = render(contract_schema(cls))
        if not path.exists():
            problems.append(f"{cls.schema_id()}: schema not exported (run `air schema export`)")
        elif path.read_text("utf-8") != expected:
            problems.append(f"{cls.schema_id()}: committed schema differs from the contract "
                            "(a published version must not change: bump the version and "
                            "export, or revert the contract change)")
        problems.extend(_compat_problems(path.parent))
    return problems


def _version_tuple(path: Path) -> tuple[int, int]:
    major, minor = path.stem.split(".")[:2]
    return int(major), int(minor)


def _compat_problems(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    versions = sorted(directory.glob("*.json"), key=_version_tuple)
    problems = []
    for older, newer in itertools.pairwise(versions):
        if _version_tuple(older)[0] != _version_tuple(newer)[0]:
            continue  # major bump: breaking changes allowed
        old = json.loads(older.read_text("utf-8"))
        new = json.loads(newer.read_text("utf-8"))
        for problem in _superset_problems(old, new, old, new, path=""):
            problems.append(f"{directory.name}: {older.stem} -> {newer.stem}: {problem}")
    return problems


def _resolve(node: dict[str, Any], doc: dict[str, Any]) -> dict[str, Any]:
    ref = node.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        return dict(doc.get("$defs", {}).get(ref.removeprefix("#/$defs/"), {}))
    return node


def _superset_problems(old: dict[str, Any], new: dict[str, Any], old_doc: dict[str, Any],
                       new_doc: dict[str, Any], path: str, depth: int = 0) -> list[str]:
    """A newer minor must accept everything the older one did."""
    if depth > 20:
        return []
    old, new = _resolve(old, old_doc), _resolve(new, new_doc)
    problems = []
    if "enum" in old and "enum" in new and not set(map(json.dumps, old["enum"])) <= set(
            map(json.dumps, new["enum"])):
        problems.append(f"{path or '/'}: enum values removed")
    if old.get("type") != new.get("type") and "type" in old and "type" in new:
        problems.append(f"{path or '/'}: type changed {old.get('type')} -> {new.get('type')}")
    if old.get("type") == "object" and "properties" in old:
        new_props = new.get("properties", {})
        for name, sub in old["properties"].items():
            if name == SCHEMA_KEY:
                continue
            if name not in new_props:
                problems.append(f"{path}/{name}: property removed")
            else:
                problems.extend(_superset_problems(sub, new_props[name], old_doc, new_doc,
                                                   f"{path}/{name}", depth + 1))
        added_required = set(new.get("required", [])) - set(old.get("required", []))
        if added_required:
            problems.append(f"{path or '/'}: new required fields {sorted(added_required)}")
    if old.get("type") == "array" and isinstance(old.get("items"), dict):
        problems.extend(_superset_problems(old["items"], new.get("items", {}), old_doc,
                                           new_doc, f"{path}[]", depth + 1))
    return problems
