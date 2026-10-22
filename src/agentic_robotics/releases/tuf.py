"""TUF-style signed metadata over registry assets, plus an Uptane-like director.

Metadata shapes follow The Update Framework specification
(theupdateframework.github.io/specification, checked 2026-10-22):

- every file is an envelope ``{"signed": {...}, "signatures": [{"keyid", "sig"}]}``;
- ``root``: ``_type, spec_version, consistent_snapshot, version, expires, keys
  {keyid: {keytype, scheme, keyval: {public}}}, roles {root|targets|snapshot|timestamp:
  {keyids, threshold}}``;
- ``targets``: ``targets {path: {length, hashes: {sha256}, custom?}}``;
- ``snapshot``: ``meta {"targets.json": {version, length?, hashes?}}``;
- ``timestamp``: ``meta {"snapshot.json": {version, length?, hashes?}}`` (only that);
- ``expires`` is ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); a keyid is the hex SHA-256 of the
  canonical key object.

:class:`TrustedSet` implements the client workflow in the spec's order with a fixed
start time: root rotation (version exactly N+1, threshold of the old *and* new root),
then timestamp (rollback: version must rise; equal means "no update"), snapshot
(hashes and version must match the timestamp; listed targets versions never decrease or
disappear), and targets (must match the snapshot). Every step checks expiry against the
start time (freeze attacks). Delegated targets roles and consistent-snapshot file naming
are not implemented (see ADR 0016).

The director (Uptane pattern) is a second repository with its own root. Its targets
metadata assigns images to robots via ``custom.robot_id``; a robot installs a target only
if the director assigns it **and** the image repository lists the same length and
hashes.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from ..core.errors import FrameworkError
from .signing import KeyStore, Signer, canonical_bytes, key_id

__requirements__ = ("REQ10", "REQ16", "F04", "G14", "G15", "D15", "D17")

SPEC_VERSION = "1.0.0"
TOP_LEVEL_ROLES = ("root", "targets", "snapshot", "timestamp")
Envelope = dict[str, Any]


class TufError(FrameworkError):
    code = "tuf_error"

    def __init__(self, reason: str, message: str, **details: Any) -> None:
        super().__init__(message, reason=reason, **details)
        self.reason = reason


# -- building metadata -------------------------------------------------------------------

def format_expires(t: float) -> str:
    return dt.datetime.fromtimestamp(int(t), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_expires(text: str) -> float:
    try:
        return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=dt.UTC).timestamp()
    except ValueError as exc:
        raise TufError("bad_expires", "expires is not YYYY-MM-DDTHH:MM:SSZ",
                       value=text) from exc


def root_metadata(role_keys: Mapping[str, Sequence[dict[str, Any]]],
                  thresholds: Mapping[str, int], *, version: int, expires: float,
                  consistent_snapshot: bool = False) -> dict[str, Any]:
    keys: dict[str, Any] = {}
    roles: dict[str, Any] = {}
    for role in TOP_LEVEL_ROLES:
        ids = []
        for k in role_keys[role]:
            kid = key_id(k)
            keys[kid] = dict(k)
            ids.append(kid)
        threshold = int(thresholds.get(role, 1))
        if threshold < 1 or threshold > len(ids):
            raise TufError("bad_threshold", "threshold must be between 1 and the key count",
                           role=role, threshold=threshold, keys=len(ids))
        roles[role] = {"keyids": sorted(ids), "threshold": threshold}
    return {"_type": "root", "spec_version": SPEC_VERSION,
            "consistent_snapshot": consistent_snapshot, "version": version,
            "expires": format_expires(expires), "keys": keys, "roles": roles}


def target_info(data: bytes, custom: Mapping[str, Any] | None = None) -> dict[str, Any]:
    info: dict[str, Any] = {"length": len(data),
                            "hashes": {"sha256": hashlib.sha256(data).hexdigest()}}
    if custom:
        info["custom"] = dict(custom)
    return info


def targets_metadata(targets: Mapping[str, dict[str, Any]], *, version: int,
                     expires: float) -> dict[str, Any]:
    return {"_type": "targets", "spec_version": SPEC_VERSION, "version": version,
            "expires": format_expires(expires), "targets": dict(targets)}


def file_meta(envelope: Envelope) -> dict[str, Any]:
    data = encode(envelope)
    return {"version": envelope["signed"]["version"], "length": len(data),
            "hashes": {"sha256": hashlib.sha256(data).hexdigest()}}


def snapshot_metadata(targets: Envelope, *, version: int, expires: float) -> dict[str, Any]:
    return {"_type": "snapshot", "spec_version": SPEC_VERSION, "version": version,
            "expires": format_expires(expires), "meta": {"targets.json": file_meta(targets)}}


def timestamp_metadata(snapshot: Envelope, *, version: int,
                       expires: float) -> dict[str, Any]:
    return {"_type": "timestamp", "spec_version": SPEC_VERSION, "version": version,
            "expires": format_expires(expires),
            "meta": {"snapshot.json": file_meta(snapshot)}}


def sign(signed: dict[str, Any], signers: Iterable[Signer]) -> Envelope:
    data = canonical_bytes(signed)
    sigs, seen = [], set()
    for s in signers:
        if s.keyid in seen:
            continue
        seen.add(s.keyid)
        sigs.append({"keyid": s.keyid, "sig": s.sign(data)})
    return {"signed": signed, "signatures": sigs}


def encode(envelope: Envelope) -> bytes:
    return canonical_bytes(envelope)


def decode(data: bytes) -> Envelope:
    try:
        env = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TufError("bad_metadata", "metadata is not JSON") from exc
    if not isinstance(env, dict) or set(env) != {"signed", "signatures"}:
        raise TufError("bad_metadata", "metadata is not a signed envelope")
    return env


# -- the client --------------------------------------------------------------------------

class TrustedSet:
    """Client-side trusted metadata, updated in the order the TUF spec prescribes."""

    def __init__(self, trusted_root: bytes, keys: KeyStore, now: float) -> None:
        self.keys = keys
        self.now = now                      # fixed update start time
        env = decode(trusted_root)
        self._check_type(env, "root")
        self._verify(env, env["signed"], "root")     # self-signed, expiry checked later
        self.root: Envelope = env
        self.timestamp: Envelope | None = None
        self.snapshot: Envelope | None = None
        self.targets: Envelope | None = None

    # -- steps ---------------------------------------------------------------------------
    def update_root(self, data: bytes) -> None:
        new = decode(data)
        self._check_type(new, "root")
        self._verify(new, self.root["signed"], "root")
        self._verify(new, new["signed"], "root")
        expected = self.root["signed"]["version"] + 1
        if new["signed"]["version"] != expected:
            raise TufError("rollback", "root version must be exactly N+1",
                           expected=expected, actual=new["signed"]["version"])
        old_roles = self.root["signed"]["roles"]
        self.root = new
        for role in ("timestamp", "snapshot"):
            if old_roles[role] != new["signed"]["roles"][role]:
                self.timestamp = self.snapshot = None   # keys rotated: forget

    def update_timestamp(self, data: bytes) -> bool:
        """Return False when the new timestamp has the same version (nothing new)."""
        self._check_expiry(self.root, "root")
        new = decode(data)
        self._check_type(new, "timestamp")
        self._verify(new, self.root["signed"], "timestamp")
        if self.timestamp is not None:
            old_v, new_v = self.timestamp["signed"]["version"], new["signed"]["version"]
            if new_v < old_v:
                raise TufError("rollback", "timestamp version decreased", trusted=old_v,
                               actual=new_v)
            if new_v == old_v:
                return False
            old_snap = self.timestamp["signed"]["meta"]["snapshot.json"]["version"]
            if new["signed"]["meta"]["snapshot.json"]["version"] < old_snap:
                raise TufError("rollback", "snapshot version in timestamp decreased")
        if set(new["signed"]["meta"]) != {"snapshot.json"}:
            raise TufError("bad_metadata", "timestamp meta must list only snapshot.json")
        self._check_expiry(new, "timestamp")
        self.timestamp = new
        return True

    def update_snapshot(self, data: bytes) -> None:
        if self.timestamp is None:
            raise TufError("order", "update the timestamp before the snapshot")
        expected = self.timestamp["signed"]["meta"]["snapshot.json"]
        self._check_file(data, expected, "snapshot.json")
        new = decode(data)
        self._check_type(new, "snapshot")
        self._verify(new, self.root["signed"], "snapshot")
        if new["signed"]["version"] != expected["version"]:
            raise TufError("mismatch", "snapshot version does not match the timestamp")
        if self.snapshot is not None:
            for name, meta in self.snapshot["signed"]["meta"].items():
                now_meta = new["signed"]["meta"].get(name)
                if now_meta is None:
                    raise TufError("rollback", "a targets file disappeared from the "
                                   "snapshot", file=name)
                if now_meta["version"] < meta["version"]:
                    raise TufError("rollback", "targets version decreased", file=name)
        self._check_expiry(new, "snapshot")
        self.snapshot = new

    def update_targets(self, data: bytes) -> None:
        if self.snapshot is None:
            raise TufError("order", "update the snapshot before targets")
        expected = self.snapshot["signed"]["meta"].get("targets.json")
        if expected is None:
            raise TufError("mismatch", "snapshot does not list targets.json")
        self._check_file(data, expected, "targets.json")
        new = decode(data)
        self._check_type(new, "targets")
        self._verify(new, self.root["signed"], "targets")
        if new["signed"]["version"] != expected["version"]:
            raise TufError("mismatch", "targets version does not match the snapshot")
        self._check_expiry(new, "targets")
        self.targets = new

    def refresh(self, *, timestamp: bytes, snapshot: bytes, targets: bytes,
                roots: Sequence[bytes] = ()) -> None:
        for r in roots:
            self.update_root(r)
        if self.update_timestamp(timestamp):
            self.update_snapshot(snapshot)
            self.update_targets(targets)
        elif self.targets is None:
            raise TufError("order", "no trusted targets yet")

    # -- targets -------------------------------------------------------------------------
    def target(self, path: str) -> dict[str, Any]:
        if self.targets is None:
            raise TufError("order", "no trusted targets metadata")
        info = self.targets["signed"]["targets"].get(path)
        if info is None:
            raise TufError("unknown_target", "target is not listed in trusted metadata",
                           path=path)
        return dict(info)

    def verify_target(self, path: str, data: bytes) -> dict[str, Any]:
        info = self.target(path)
        self._check_file(data, info, path)
        return info

    def all_targets(self) -> dict[str, dict[str, Any]]:
        if self.targets is None:
            raise TufError("order", "no trusted targets metadata")
        return dict(self.targets["signed"]["targets"])

    # -- internals -----------------------------------------------------------------------
    @staticmethod
    def _check_type(env: Envelope, role: str) -> None:
        signed = env.get("signed")
        if not isinstance(signed, dict) or signed.get("_type") != role:
            raise TufError("bad_metadata", f"expected {role} metadata",
                           actual=signed.get("_type") if isinstance(signed, dict) else None)
        if str(signed.get("spec_version", "")).split(".")[0] != SPEC_VERSION.split(".")[0]:
            raise TufError("bad_metadata", "unsupported spec_version",
                           actual=signed.get("spec_version"))

    def _verify(self, env: Envelope, root_signed: dict[str, Any], role: str) -> None:
        spec = root_signed["roles"][role]
        allowed = set(spec["keyids"])
        data = canonical_bytes(env["signed"])
        good: set[str] = set()
        for s in env.get("signatures", []):
            kid = s.get("keyid")
            if kid in good or kid not in allowed:
                continue
            key = root_signed["keys"].get(kid)
            if key is None or key_id(key) != kid:
                continue
            if self.keys.verify(key, str(s.get("sig", "")), data):
                good.add(kid)
        if len(good) < spec["threshold"]:
            raise TufError("threshold", f"{role} metadata lacks a threshold of valid "
                           "signatures", role=role, valid=len(good),
                           threshold=spec["threshold"])

    def _check_expiry(self, env: Envelope, role: str) -> None:
        if parse_expires(env["signed"]["expires"]) <= self.now:
            raise TufError("expired", f"{role} metadata has expired", role=role,
                           expires=env["signed"]["expires"])

    @staticmethod
    def _check_file(data: bytes, expected: Mapping[str, Any], name: str) -> None:
        length = expected.get("length")
        if length is not None and len(data) != length:
            raise TufError("mismatch", "length does not match trusted metadata", file=name,
                           expected=length, actual=len(data))
        for algo, digest in (expected.get("hashes") or {}).items():
            if algo != "sha256":
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                raise TufError("mismatch", "hash does not match trusted metadata", file=name)


# -- registry assets ---------------------------------------------------------------------

def asset_bytes(document: Mapping[str, Any]) -> bytes:
    """The bytes a registry asset is hashed as (same encoding as ``document_digest``)."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def registry_targets(registry: Any) -> dict[str, dict[str, Any]]:
    """TUF target entries for every asset of a :class:`LocalRegistry` (path
    ``<name>/<version>.json``). Each asset is fetched, so integrity is checked first."""
    out: dict[str, dict[str, Any]] = {}
    for ref in registry.list():
        doc = registry.fetch(ref.name, ref.version).to_dict()
        out[f"{ref.name}/{ref.version}.json"] = target_info(
            asset_bytes(doc), {"schema": ref.schema, "air_ref": str(ref)})
    return out


def verify_registry_asset(trusted: TrustedSet, registry: Any, name: str,
                          version: str) -> Any:
    """Fetch an asset and require that it matches the trusted targets metadata."""
    asset = registry.fetch(name, version)
    trusted.verify_target(f"{name}/{version}.json", asset_bytes(asset.to_dict()))
    return asset


# -- director (Uptane pattern) -----------------------------------------------------------

def director_targets(assignments: Mapping[str, Mapping[str, tuple[str, dict[str, Any]]]]
                     ) -> dict[str, dict[str, Any]]:
    """``{robot_id: {slot: (path, image_target_info)}}`` -> director target entries.

    Director paths are ``<robot_id>/<slot>`` so one robot's assignment can never be
    read as another's."""
    out: dict[str, dict[str, Any]] = {}
    for robot_id, slots in assignments.items():
        for slot, (path, info) in slots.items():
            out[f"{robot_id}/{slot}"] = {
                "length": info["length"], "hashes": dict(info["hashes"]),
                "custom": {"robot_id": robot_id, "slot": slot, "image_path": path}}
    return out


def assigned_targets(director: TrustedSet, image: TrustedSet,
                     robot_id: str) -> dict[str, dict[str, Any]]:
    """``{slot: image_target_info}`` for ``robot_id``; both repositories must agree."""
    out: dict[str, dict[str, Any]] = {}
    for dpath, dinfo in director.all_targets().items():
        custom = dinfo.get("custom") or {}
        if custom.get("robot_id") != robot_id:
            continue
        if dpath != f"{robot_id}/{custom.get('slot')}":
            raise TufError("mismatch", "director path does not match its robot and slot",
                           path=dpath)
        ipath = str(custom.get("image_path", ""))
        info = image.target(ipath)
        if info["length"] != dinfo["length"] or info["hashes"] != dinfo["hashes"]:
            raise TufError("mismatch", "director and image repository disagree",
                           robot_id=robot_id, path=ipath)
        out[str(custom["slot"])] = {**info, "path": ipath}
    return out
