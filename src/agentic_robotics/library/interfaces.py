"""Interface catalog: the typed APIs packages provide and require.

Built-in interfaces ship as JSON next to this module (``data/interfaces.json``); a
project adds its own in ``.air/interfaces/*.json``. One interface id may exist at several
versions. Compatibility is semver: a provider of ``1.4.0`` satisfies a requirement of
``^1.2``; ``2.0.0`` does not.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from ..core.errors import SchemaError
from ..core.versions import satisfies, version_key
from .contracts import InterfaceContract

__requirements__ = ("REQ05", "REQ18")

DATA = Path(__file__).with_name("data") / "interfaces.json"


class InterfaceCatalog:
    def __init__(self, contracts: Iterable[InterfaceContract] = ()) -> None:
        self._by_id: dict[str, dict[str, InterfaceContract]] = defaultdict(dict)
        for c in contracts:
            self.add(c)

    def add(self, c: InterfaceContract) -> None:
        existing = self._by_id[c.interface_id].get(c.version)
        if existing is not None and existing != c:
            raise SchemaError("interface version already defined differently",
                              interface=c.interface_id, version=c.version)
        self._by_id[c.interface_id][c.version] = c

    def ids(self) -> list[str]:
        return sorted(self._by_id)

    def versions(self, interface_id: str) -> list[str]:
        return sorted(self._by_id.get(interface_id, {}), key=version_key)

    def get(self, interface_id: str, version: str | None = None) -> InterfaceContract | None:
        versions = self._by_id.get(interface_id)
        if not versions:
            return None
        if version is None:
            return versions[self.versions(interface_id)[-1]]
        return versions.get(version)

    def best(self, interface_id: str, spec: str) -> InterfaceContract | None:
        """Highest catalog version of an interface inside a range."""
        ok = [v for v in self.versions(interface_id) if satisfies(v, spec)]
        return self._by_id[interface_id][ok[-1]] if ok else None

    def __iter__(self):  # type: ignore[no-untyped-def]
        for versions in self._by_id.values():
            yield from versions.values()

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_id.values())


def load_interface_files(paths: Iterable[str | Path]) -> list[InterfaceContract]:
    out = []
    for path in paths:
        doc = json.loads(Path(path).read_text("utf-8"))
        for item in doc.get("interfaces", []):
            out.append(InterfaceContract.from_dict({"$schema": "ar.interface/1.0", **item}))
    return out


def builtin_interfaces() -> InterfaceCatalog:
    return InterfaceCatalog(load_interface_files([DATA]) if DATA.exists() else [])


def project_interfaces(root: str | Path = ".") -> InterfaceCatalog:
    cat = builtin_interfaces()
    for c in load_interface_files(sorted((Path(root) / ".air" / "interfaces").glob("*.json"))):
        cat.add(c)
    return cat
