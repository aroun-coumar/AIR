"""Versioned, typed contract base.

Contracts are frozen dataclasses with a registered schema name and semantic version.
They serialize to plain JSON-compatible dictionaries and are parsed back strictly:
unknown fields, wrong types, and incompatible major versions raise :class:`SchemaError`
instead of being silently ignored.

    @contract("observation", version="1.0")
    class Observation(Contract):
        source: str
        value: float
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, TypeVar, Union, get_args, get_origin

from .errors import IncompatibleSchemaVersion, SchemaError

__requirements__ = ("REQ01", "REQ18")

SCHEMA_KEY = "$schema"
SCHEMA_NAMESPACE = "ar"

C = TypeVar("C", bound="Contract")

_REGISTRY: dict[str, type[Contract]] = {}
_HINTS: dict[type, dict[str, Any]] = {}


@dataclasses.dataclass(frozen=True)
class Meta:
    """Provenance, access scope and revision carried by every contract (baseline section 6)."""

    source: str = "unspecified"
    access_scope: str = "project"
    revision: int = 1
    created_at: float | None = None
    derived_from: tuple[str, ...] = ()


@dataclasses.dataclass(frozen=True, kw_only=True)
class Contract:
    SCHEMA: ClassVar[str] = ""
    VERSION: ClassVar[str] = "1.0"

    meta: Meta = dataclasses.field(default_factory=Meta)

    # -- serialization -------------------------------------------------------------
    @classmethod
    def schema_id(cls) -> str:
        return f"{SCHEMA_NAMESPACE}.{cls.SCHEMA}/{cls.VERSION}"

    def to_dict(self) -> dict[str, Any]:
        body = _dump(self)
        assert isinstance(body, dict)
        return {SCHEMA_KEY: self.schema_id(), **body}

    def to_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), **kwargs)

    @classmethod
    def from_dict(cls: type[C], data: Mapping[str, Any]) -> C:
        if not isinstance(data, Mapping):
            raise SchemaError("expected an object", contract=cls.SCHEMA)
        declared = data.get(SCHEMA_KEY)
        if declared is not None:
            _check_schema_id(cls, str(declared))
        payload = {k: v for k, v in data.items() if k != SCHEMA_KEY}
        return typing.cast(C, _load(cls, payload, cls.SCHEMA))

    @classmethod
    def from_json(cls: type[C], text: str) -> C:
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise SchemaError("invalid JSON", contract=cls.SCHEMA, error=str(exc)) from exc

    @classmethod
    def load(cls: type[C], path: str | Path) -> C:
        p = Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise SchemaError("cannot read contract file", path=str(p), error=str(exc)) from exc
        return cls.from_json(text)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(self.to_json(indent=2) + "\n", encoding="utf-8")

    def evolve(self: C, **changes: Any) -> C:
        """Return a copy with ``changes`` applied (contracts are immutable)."""
        return dataclasses.replace(self, **changes)


@typing.dataclass_transform(kw_only_default=True, frozen_default=True,
                            field_specifiers=(dataclasses.field,))
def contract(name: str, version: str = "1.0") -> Callable[[type[C]], type[C]]:
    """Class decorator: make ``cls`` a frozen, keyword-only dataclass contract."""

    def wrap(cls: type[C]) -> type[C]:
        cls.SCHEMA = name
        cls.VERSION = version
        dc = dataclasses.dataclass(frozen=True, kw_only=True)(cls)
        if name in _REGISTRY and _REGISTRY[name].__qualname__ != dc.__qualname__:
            raise SchemaError("schema name registered twice", schema=name)
        _REGISTRY[name] = dc
        return dc

    return wrap


def registered_contracts() -> dict[str, type[Contract]]:
    return dict(_REGISTRY)


def load_any(data: Mapping[str, Any]) -> Contract:
    """Parse a document using its ``$schema`` field to select the contract type."""
    declared = data.get(SCHEMA_KEY)
    if not isinstance(declared, str):
        raise SchemaError(f"document has no {SCHEMA_KEY} field")
    name = declared.split("/", 1)[0].removeprefix(f"{SCHEMA_NAMESPACE}.")
    cls = _REGISTRY.get(name)
    if cls is None:
        raise SchemaError("unknown schema", schema=declared)
    return cls.from_dict(data)


# -- internals ---------------------------------------------------------------------


def _check_schema_id(cls: type[Contract], declared: str) -> None:
    name, _, version = declared.partition("/")
    if name != f"{SCHEMA_NAMESPACE}.{cls.SCHEMA}":
        raise SchemaError("document declares a different schema", expected=cls.schema_id(),
                          actual=declared)
    if version.split(".")[0] != cls.VERSION.split(".")[0]:
        raise IncompatibleSchemaVersion("major schema version mismatch",
                                        expected=cls.VERSION, actual=version)


def _hints(cls: type) -> dict[str, Any]:
    if cls not in _HINTS:
        _HINTS[cls] = typing.get_type_hints(cls)
    return _HINTS[cls]


def _dump(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            if v is None:
                continue
            out[f.name] = _dump(v)
        return out
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_dump(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _dump(v) for k, v in value.items()}
    return value


def _load(tp: Any, value: Any, path: str) -> Any:
    if tp is Any:
        return value
    origin = get_origin(tp)

    if origin in (Union, types.UnionType):
        options = get_args(tp)
        if value is None:
            if type(None) in options:
                return None
            raise SchemaError("value is required", path=path)
        errors = []
        for opt in options:
            if opt is type(None):
                continue
            try:
                return _load(opt, value, path)
            except SchemaError as exc:
                errors.append(str(exc))
        raise SchemaError("value matches no allowed type", path=path, errors=errors)

    if origin is typing.Literal:
        if value not in get_args(tp):
            raise SchemaError("value not allowed", path=path, allowed=list(get_args(tp)))
        return value

    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)):
            raise SchemaError("expected a list", path=path)
        args = get_args(tp)
        item_tp = args[0] if args else Any
        items = [_load(item_tp, v, f"{path}[{i}]") for i, v in enumerate(value)]
        return tuple(items) if origin is tuple else items

    if origin in (dict, Mapping) or tp is dict:
        if not isinstance(value, Mapping):
            raise SchemaError("expected an object", path=path)
        args = get_args(tp)
        val_tp = args[1] if len(args) == 2 else Any
        return {str(k): _load(val_tp, v, f"{path}.{k}") for k, v in value.items()}

    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value)
        except ValueError:
            raise SchemaError("invalid enum value", path=path, value=value,
                              allowed=[m.value for m in tp]) from None

    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        if isinstance(value, tp):
            return value
        if not isinstance(value, Mapping):
            raise SchemaError("expected an object", path=path)
        hints = _hints(tp)
        fields = {f.name: f for f in dataclasses.fields(tp)}
        unknown = set(value) - set(fields) - {SCHEMA_KEY}
        if unknown:
            raise SchemaError("unknown fields", path=path, fields=sorted(unknown))
        kwargs: dict[str, Any] = {}
        for name, f in fields.items():
            if name in value:
                kwargs[name] = _load(hints[name], value[name], f"{path}.{name}")
            elif f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:
                raise SchemaError("missing required field", path=f"{path}.{name}")
        try:
            return tp(**kwargs)
        except (TypeError, ValueError) as exc:
            raise SchemaError("invalid value", path=path, error=str(exc)) from exc

    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SchemaError("expected a number", path=path)
        return float(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError("expected an integer", path=path)
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise SchemaError("expected a boolean", path=path)
        return value
    if tp is str:
        if not isinstance(value, str):
            raise SchemaError("expected a string", path=path)
        return value
    if isinstance(tp, type) and isinstance(value, tp):
        return value
    raise SchemaError("unsupported field type", path=path, type=str(tp))
