"""Local asset registry: the platform layer's storage contract (baseline section 18).

Stores versioned contract documents (manifests, skill sets, plans, releases ...) as
immutable, content-addressed files. Works offline on a plain directory; a hosted
registry or workbench implements the same :class:`AssetRegistry` interface.

Publishing, installing, qualifying and activating are distinct steps. Fetching an asset
never executes code and never authorizes physical operation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core.errors import SchemaError
from .core.ids import is_valid_id
from .core.schema import SCHEMA_KEY, Contract, load_any

__requirements__ = ("REQ10", "REQ18")


@dataclass(frozen=True)
class AssetRef:
    name: str
    version: str
    schema: str
    digest: str

    def __str__(self) -> str:
        return f"{self.name}@{self.version}#{self.digest[:12]}"


class AssetRegistry(Protocol):
    def publish(self, name: str, version: str, asset: Contract) -> AssetRef: ...
    def fetch(self, name: str, version: str | None = None) -> Contract: ...
    def versions(self, name: str) -> list[str]: ...
    def list(self) -> list[AssetRef]: ...


def _digest(doc: dict[str, Any]) -> str:
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


class LocalRegistry:
    """Directory-backed registry: ``<root>/<name>/<version>.json`` plus an index."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def publish(self, name: str, version: str, asset: Contract) -> AssetRef:
        if (not is_valid_id(name) or not is_valid_id(version)
                or any(c in name + version for c in "/:")):
            raise SchemaError("invalid asset name or version", name=name, version=version)
        doc = asset.to_dict()
        ref = AssetRef(name, version, str(doc[SCHEMA_KEY]), _digest(doc))
        path = self._path(name, version)
        if path.exists():
            existing = json.loads(path.read_text("utf-8"))
            if existing["digest"] == ref.digest:
                return ref
            raise SchemaError("asset versions are immutable; publish a new version",
                              name=name, version=version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ref": ref.__dict__, "digest": ref.digest, "document": doc},
                                   indent=2) + "\n", encoding="utf-8")
        return ref

    def fetch(self, name: str, version: str | None = None) -> Contract:
        version = version or self._latest(name)
        path = self._path(name, version)
        if not path.exists():
            raise SchemaError("asset not found", name=name, version=version)
        record = json.loads(path.read_text("utf-8"))
        doc = record["document"]
        if _digest(doc) != record["digest"]:
            raise SchemaError("asset integrity check failed", name=name, version=version)
        return load_any(doc)

    def versions(self, name: str) -> list[str]:
        d = self.root / name
        if not d.is_dir():
            return []
        return sorted((p.stem for p in d.glob("*.json")), key=_version_key)

    def list(self) -> list[AssetRef]:
        out = []
        for path in sorted(self.root.glob("*/*.json")):
            r = json.loads(path.read_text("utf-8"))["ref"]
            out.append(AssetRef(**r))
        return out

    def _latest(self, name: str) -> str:
        vs = self.versions(name)
        if not vs:
            raise SchemaError("asset not found", name=name)
        return vs[-1]

    def _path(self, name: str, version: str) -> Path:
        return self.root / name / f"{version}.json"


def _version_key(v: str) -> tuple[Any, ...]:
    parts: list[Any] = []
    for p in v.replace("-", ".").split("."):
        parts.append((0, int(p)) if p.isdigit() else (1, p))
    return tuple(parts)
