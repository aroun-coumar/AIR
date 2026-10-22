"""Local asset registry: the platform layer's storage contract (baseline section 18).

Stores versioned contract documents (manifests, skill sets, plans, releases ...) as
immutable, content-addressed files. Works offline on a plain directory; a hosted
registry or workbench implements the same :class:`AssetRegistry` interface.

Integrity does not rely on the asset file vouching for itself. Every publish is
appended to a hash-chained publish log (``publish-log.jsonl``), and a fetch checks the
document against the digest recorded *there*, and against a caller-pinned digest or
lockfile when given. Editing a document together with its stored digest is detected.
(Rewriting the publish log itself consistently is caught only by external anchoring of
its head, which arrives with ledger checkpoints.)

Publishing, installing, qualifying and activating are distinct steps. Fetching an asset
never executes code and never authorizes physical operation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .core.errors import SchemaError
from .core.ids import is_valid_id
from .core.schema import SCHEMA_KEY, Contract, load_any
from .core.versions import is_prerelease, version_key
from .provenance.ledger import Ledger

__all__ = ["AssetRef", "AssetRegistry", "LocalRegistry", "document_digest", "is_prerelease",
           "version_key"]

__requirements__ = ("REQ10", "REQ18", "G15")

PUBLISH_LOG = "publish-log.jsonl"
LOCK_FILE = "air.lock"


@dataclass(frozen=True)
class AssetRef:
    name: str
    version: str
    schema: str
    digest: str

    def __str__(self) -> str:
        return f"{self.name}@{self.version}#{self.digest}"

    @classmethod
    def parse(cls, text: str) -> tuple[str, str | None, str | None]:
        """Parse ``name[@version][#digest]`` into its parts."""
        rest, _, digest = text.partition("#")
        name, _, version = rest.partition("@")
        return name, version or None, digest or None


class AssetRegistry(Protocol):
    def publish(self, name: str, version: str, asset: Contract) -> AssetRef: ...
    def fetch(self, name: str, version: str | None = None, *, digest: str | None = None,
              include_prerelease: bool = False) -> Contract: ...
    def versions(self, name: str, include_prerelease: bool = True) -> list[str]: ...
    def list(self) -> list[AssetRef]: ...


def document_digest(doc: dict[str, Any]) -> str:
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class LocalRegistry:
    """Directory-backed registry: ``<root>/<name>/<version>.json`` plus a publish log."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = Ledger(self.root / PUBLISH_LOG)

    # -- publish -------------------------------------------------------------------------
    def publish(self, name: str, version: str, asset: Contract) -> AssetRef:
        if (not is_valid_id(name) or not is_valid_id(version)
                or any(c in name + version for c in "/:#@")):
            raise SchemaError("invalid asset name or version", name=name, version=version)
        doc = asset.to_dict()
        ref = AssetRef(name, version, str(doc[SCHEMA_KEY]), document_digest(doc))
        path = self._path(name, version)
        if path.exists():
            if self._logged_digest(name, version) == ref.digest:
                return ref
            raise SchemaError("asset versions are immutable; publish a new version",
                              name=name, version=version)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ref": ref.__dict__, "document": doc}, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
        self.log.append("asset.published", f"{name}@{version}",
                        {"digest": ref.digest, "schema": ref.schema})
        return ref

    # -- fetch ---------------------------------------------------------------------------
    def fetch(self, name: str, version: str | None = None, *, digest: str | None = None,
              include_prerelease: bool = False) -> Contract:
        version = version or self.latest(name, include_prerelease=include_prerelease)
        path = self._path(name, version)
        if not path.exists():
            raise SchemaError("asset not found", name=name, version=version)
        record = json.loads(path.read_text("utf-8"))
        doc = record["document"]
        actual = document_digest(doc)
        logged = self._logged_digest(name, version)
        if logged is None:
            raise SchemaError("asset is not in the publish log", name=name, version=version)
        if actual != logged:
            raise SchemaError("asset integrity check failed: document does not match the "
                              "digest recorded at publish", name=name, version=version,
                              expected=logged, actual=actual)
        if digest is not None and digest != actual:
            raise SchemaError("asset digest does not match the pinned digest",
                              name=name, version=version, pinned=digest, actual=actual)
        return load_any(doc)

    def resolve(self, ref: str, include_prerelease: bool = False) -> Contract:
        """Fetch by ``name[@version][#digest]``."""
        name, version, digest = AssetRef.parse(ref)
        return self.fetch(name, version, digest=digest, include_prerelease=include_prerelease)

    def verify_log(self) -> bool:
        return self.log.verify().ok

    # -- queries -------------------------------------------------------------------------
    def versions(self, name: str, include_prerelease: bool = True) -> list[str]:
        d = self.root / name
        if not d.is_dir():
            return []
        vs = [p.stem for p in d.glob("*.json")]
        if not include_prerelease:
            vs = [v for v in vs if not is_prerelease(v)]
        return sorted(vs, key=version_key)

    def latest(self, name: str, include_prerelease: bool = False) -> str:
        vs = self.versions(name, include_prerelease=include_prerelease)
        if not vs:
            hint = (" (only prereleases exist; pass include_prerelease=True)"
                    if self.versions(name) else "")
            raise SchemaError("asset not found" + hint, name=name)
        return vs[-1]

    def list(self) -> list[AssetRef]:
        out = []
        for path in sorted(self.root.glob("*/*.json")):
            out.append(AssetRef(**json.loads(path.read_text("utf-8"))["ref"]))
        return out

    # -- lockfile ------------------------------------------------------------------------
    def lock(self, refs: Sequence[str], path: str | Path = LOCK_FILE) -> dict[str, str]:
        """Resolve refs now and write ``{name: "name@version#digest"}`` to a lockfile."""
        locked: dict[str, str] = {}
        for text in refs:
            name, version, digest = AssetRef.parse(text)
            version = version or self.latest(name)
            self.fetch(name, version, digest=digest)
            locked[name] = f"{name}@{version}#{self._logged_digest(name, version)}"
        Path(path).write_text(json.dumps({"assets": locked}, indent=2, sort_keys=True) + "\n",
                              encoding="utf-8", newline="\n")
        return locked

    def fetch_locked(self, name: str, path: str | Path = LOCK_FILE) -> Contract:
        assets = json.loads(Path(path).read_text("utf-8")).get("assets", {})
        if name not in assets:
            raise SchemaError("asset is not in the lockfile", name=name, lockfile=str(path))
        return self.resolve(assets[name], include_prerelease=True)

    # -- internals -----------------------------------------------------------------------
    def _logged_digest(self, name: str, version: str) -> str | None:
        entries = self.log.query(kind="asset.published", subject=f"{name}@{version}")
        return str(entries[-1].data["digest"]) if entries else None

    def _path(self, name: str, version: str) -> Path:
        return self.root / name / f"{version}.json"
