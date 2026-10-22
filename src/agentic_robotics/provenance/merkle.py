"""RFC 9162 Merkle tree over the ledger, with C2SP signed checkpoints.

The hash chain in :mod:`.ledger` detects edits, deletions and reordering *inside* the
log. It cannot detect a truncated tail or a wholesale rewrite: a rewritten chain is
internally consistent. A checkpoint fixes that. It is a small text file (C2SP
``tlog-checkpoint``) naming the tree size and the RFC 9162 Merkle root over the first
``size`` entry hashes. Once a checkpoint is committed to git (or anchored elsewhere), any
later ledger must contain exactly those entries as a prefix, which a **consistency
proof** shows in O(log n) hashes. An **inclusion proof** shows that one entry is in the
tree without the rest of the file.

Leaves are the entries' own SHA-256 hashes (``LedgerEntry.hash``), so a verifier first
recomputes an entry's hash from its content (RFC 8785, see :mod:`.ledger`), then the
leaf hash ``SHA-256(0x00 || entry_hash)``, then walks the proof. Interior nodes are
``SHA-256(0x01 || left || right)`` (RFC 9162 section 2.1.1 domain separation).

Signing is optional (see :mod:`.signing`): unsigned, HMAC, or Ed25519. A writer must
never sign two inconsistent checkpoints, so :func:`create_checkpoint` refuses to write
one that is not an extension of the latest stored checkpoint.

External anchoring: git is the anchor (commit ``.air/checkpoints/``). The
:class:`Anchor` protocol is where a transparency log such as Rekor would plug in; AIR
ships no network anchor (owner decision), only :class:`GitAnchor` and the
:class:`RekorAnchor` placeholder that refuses.
"""

from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..core.errors import FrameworkError, SchemaError, UnsupportedOperation
from .signing import Signer, Verifier, b64decode_canonical

if TYPE_CHECKING:
    from .ledger import Ledger
    from .project import Project

__requirements__ = ("REQ10", "REQ16")

CHECKPOINT_DIR = "checkpoints"
EMPTY_ROOT = hashlib.sha256(b"").digest()


# -- RFC 9162 section 2.1 ---------------------------------------------------------------
def leaf_hash(data: bytes) -> bytes:
    return hashlib.sha256(b"\x00" + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _split(n: int) -> int:
    """Largest power of two strictly smaller than ``n`` (n >= 2)."""
    k = 1
    while k << 1 < n:
        k <<= 1
    return k


class MerkleTree:
    """A Merkle tree over a list of leaf *inputs* (here: ledger entry hashes)."""

    def __init__(self, leaves: Sequence[bytes]) -> None:
        self._leaves = [leaf_hash(d) for d in leaves]
        self._memo: dict[tuple[int, int], bytes] = {}

    def __len__(self) -> int:
        return len(self._leaves)

    def _mth(self, lo: int, hi: int) -> bytes:
        n = hi - lo
        if n == 0:
            return EMPTY_ROOT
        if n == 1:
            return self._leaves[lo]
        key = (lo, hi)
        cached = self._memo.get(key)
        if cached is None:
            k = _split(n)
            cached = node_hash(self._mth(lo, lo + k), self._mth(lo + k, hi))
            self._memo[key] = cached
        return cached

    def root(self, size: int | None = None) -> bytes:
        size = len(self) if size is None else size
        if not 0 <= size <= len(self):
            raise SchemaError("tree size out of range", size=size, leaves=len(self))
        return self._mth(0, size)

    def leaf(self, index: int) -> bytes:
        return self._leaves[index]

    def inclusion_proof(self, index: int, size: int | None = None) -> list[bytes]:
        """RFC 9162 2.1.3.1 ``PATH(m, D[n])``."""
        size = len(self) if size is None else size
        if not 0 <= index < size <= len(self):
            raise SchemaError("leaf index out of range", index=index, size=size)

        def path(m: int, lo: int, hi: int) -> list[bytes]:
            n = hi - lo
            if n == 1:
                return []
            k = _split(n)
            if m < k:
                return [*path(m, lo, lo + k), self._mth(lo + k, hi)]
            return [*path(m - k, lo + k, hi), self._mth(lo, lo + k)]

        return path(index, 0, size)

    def consistency_proof(self, old_size: int, new_size: int | None = None) -> list[bytes]:
        """RFC 9162 2.1.4.1 ``PROOF(m, D[n])``; empty when ``m`` is 0 or equals ``n``."""
        new_size = len(self) if new_size is None else new_size
        if not 0 <= old_size <= new_size <= len(self):
            raise SchemaError("consistency sizes out of range", old=old_size, new=new_size)
        if old_size in (0, new_size):
            return []

        def sub(m: int, lo: int, hi: int, complete: bool) -> list[bytes]:
            n = hi - lo
            if m == n:
                return [] if complete else [self._mth(lo, hi)]
            k = _split(n)
            if m <= k:
                return [*sub(m, lo, lo + k, complete), self._mth(lo + k, hi)]
            return [*sub(m - k, lo + k, hi, False), self._mth(lo, lo + k)]

        return sub(old_size, 0, new_size, True)


def verify_inclusion(index: int, size: int, leaf: bytes, proof: Sequence[bytes],
                     root: bytes) -> bool:
    """RFC 9162 2.1.3.2. ``leaf`` is the leaf *hash*."""
    if index >= size:
        return False
    fn, sn, r = index, size - 1, leaf
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = node_hash(p, r)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            r = node_hash(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


def verify_consistency(old_size: int, new_size: int, old_root: bytes, new_root: bytes,
                       proof: Sequence[bytes]) -> bool:
    """RFC 9162 2.1.4.2, plus the trivial cases (empty old tree, equal sizes)."""
    if old_size > new_size:
        return False
    if old_size == new_size:
        return not proof and old_root == new_root
    if old_size == 0:
        return not proof
    if not proof:
        return False
    path = list(proof)
    if old_size & (old_size - 1) == 0:  # exact power of two
        path.insert(0, old_root)
    fn, sn = old_size - 1, new_size - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not fn & 1:
                while fn and not fn & 1:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1
    return fr == old_root and sr == new_root and sn == 0


def ledger_tree(ledger: Ledger) -> MerkleTree:
    return MerkleTree([bytes.fromhex(e.hash) for e in ledger])


# -- C2SP tlog-checkpoint / signed-note ----------------------------------------------------
_ORIGIN = re.compile(r"^[^\s+]+$")


@dataclass(frozen=True)
class NoteSignature:
    name: str
    key_id: bytes
    signature: bytes

    def line(self) -> str:
        return f"— {self.name} {base64.b64encode(self.key_id + self.signature).decode()}"


@dataclass(frozen=True)
class Checkpoint:
    origin: str
    size: int
    root: bytes
    signatures: tuple[NoteSignature, ...] = ()

    def __post_init__(self) -> None:
        if not _ORIGIN.match(self.origin):
            raise SchemaError("checkpoint origin must be one token without spaces or '+'",
                              origin=self.origin)
        if self.size < 0 or len(self.root) != 32:
            raise SchemaError("checkpoint needs a non-negative size and a 32-byte root")

    @property
    def note_text(self) -> bytes:
        """The signed text: origin, size, base64 root, each ending in a newline."""
        return (f"{self.origin}\n{self.size}\n"
                f"{base64.b64encode(self.root).decode()}\n").encode()

    def sign(self, *signers: Signer) -> Checkpoint:
        sigs = list(self.signatures)
        for s in signers:
            sigs.append(NoteSignature(s.name, s.key_id, s.sign(self.note_text)))
        return Checkpoint(self.origin, self.size, self.root, tuple(sigs))

    def to_text(self) -> str:
        text = self.note_text.decode()
        if self.signatures:
            text += "\n" + "".join(s.line() + "\n" for s in self.signatures)
        return text

    @classmethod
    def parse(cls, text: str) -> Checkpoint:
        if any(ord(c) < 0x20 and c != "\n" for c in text):
            raise SchemaError("checkpoint contains control characters")
        body, sep, sig_block = text.rpartition("\n\n")
        if not sep:
            body, sig_block = text, ""
        else:
            body += "\n"
        lines = body.split("\n")
        if lines[-1] != "" or len(lines) < 4:
            raise SchemaError("checkpoint note text needs origin, size and root lines")
        origin, size_text, root_text = lines[0], lines[1], lines[2]
        if lines[3:-1]:
            raise SchemaError("checkpoint extension lines are not supported")
        if not re.fullmatch(r"0|[1-9][0-9]*", size_text):
            raise SchemaError("checkpoint size is not a canonical decimal", size=size_text)
        sigs = []
        for line in sig_block.splitlines():
            if not line:
                continue
            m = re.fullmatch("— (\\S+) (\\S+)", line)
            if not m:
                raise SchemaError("malformed signature line", line=line)
            raw = b64decode_canonical(m.group(2))
            if len(raw) < 5:
                raise SchemaError("signature too short", line=line)
            sigs.append(NoteSignature(m.group(1), raw[:4], raw[4:]))
        return cls(origin, int(size_text), b64decode_canonical(root_text), tuple(sigs))

    def check_signatures(self, verifiers: Iterable[Verifier]) -> SignatureReport:
        known = {(v.name, v.key_id): v for v in verifiers}
        verified, failed, unknown = [], [], []
        for s in self.signatures:
            v = known.get((s.name, s.key_id))
            if v is None:
                unknown.append(s.name)
            elif v.verify(self.note_text, s.signature):
                verified.append(f"{s.name} ({v.algorithm})")
            else:
                failed.append(s.name)
        return SignatureReport(tuple(verified), tuple(failed), tuple(unknown),
                               bool(self.signatures))


@dataclass(frozen=True)
class SignatureReport:
    verified: tuple[str, ...]
    failed: tuple[str, ...]
    unknown: tuple[str, ...]
    signed: bool

    @property
    def ok(self) -> bool:
        """At least one known key verified and none failed (signed-note rules)."""
        return bool(self.verified) and not self.failed

    def describe(self) -> str:
        if not self.signed:
            return "UNSIGNED: only its place in git history vouches for this checkpoint"
        if self.failed:
            return f"BAD SIGNATURE from {', '.join(self.failed)}"
        if self.verified:
            return f"signature verified: {', '.join(self.verified)}"
        return (f"signed by {', '.join(self.unknown)}, but no matching key was given "
                "(signature NOT checked)")


# -- checkpoints of a project ledger --------------------------------------------------------
def default_origin(project: Project) -> str:
    name = re.sub(r"[^A-Za-z0-9._/-]+", "-", project.name).strip("-") or "project"
    return f"air.local/{name}"


def checkpoint_dir(project: Project) -> Path:
    return project.air_dir / CHECKPOINT_DIR


def stored_checkpoints(project: Project) -> list[tuple[Path, Checkpoint]]:
    d = checkpoint_dir(project)
    out = [(p, Checkpoint.parse(p.read_text("utf-8"))) for p in sorted(d.glob("*.checkpoint"))]
    return sorted(out, key=lambda pc: pc[1].size)


def latest_checkpoint(project: Project) -> tuple[Path, Checkpoint] | None:
    stored = stored_checkpoints(project)
    return stored[-1] if stored else None


@dataclass
class IntegrityReport:
    ok: bool
    entries: int
    checkpoint_size: int
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    consistency_proof: list[str] = field(default_factory=list)
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "entries": self.entries,
                "checkpoint_size": self.checkpoint_size, "problems": self.problems,
                "notes": self.notes, "signature": self.signature,
                "consistency_proof": self.consistency_proof}


def verify_against(project: Project, checkpoint: Checkpoint,
                   verifiers: Iterable[Verifier] = ()) -> IntegrityReport:
    """Is the current ledger an append-only extension of ``checkpoint``?

    Detects what the chain alone cannot: a truncated tail (fewer entries than the
    checkpoint) and a rewrite (the first ``size`` entries no longer hash to its root).
    """
    chain = project.ledger.verify()
    report = IntegrityReport(True, chain.entries, checkpoint.size)
    if not chain.ok:
        report.problems.append(f"hash chain broken at seq {chain.first_bad_seq}: "
                               f"{chain.problem}")
    sig = checkpoint.check_signatures(verifiers)
    report.signature = sig.describe()
    if sig.failed:
        report.problems.append(report.signature)
    if checkpoint.origin != default_origin(project):
        report.notes.append(f"checkpoint origin {checkpoint.origin!r} differs from this "
                            f"project's {default_origin(project)!r}")
    if chain.entries < checkpoint.size:
        report.problems.append(f"ledger truncated: checkpoint covers {checkpoint.size} "
                               f"entries, ledger has {chain.entries}")
    else:
        tree = ledger_tree(project.ledger)
        if tree.root(checkpoint.size) != checkpoint.root:
            report.problems.append(f"ledger rewritten: the first {checkpoint.size} entries "
                                   "no longer match the checkpoint root")
        else:
            proof = tree.consistency_proof(checkpoint.size)
            if not verify_consistency(checkpoint.size, len(tree), checkpoint.root,
                                      tree.root(), proof):
                report.problems.append("consistency proof failed")  # pragma: no cover
            report.consistency_proof = [p.hex() for p in proof]
            report.notes.append(f"{len(tree) - checkpoint.size} entries appended since "
                                "the checkpoint")
    report.ok = not report.problems
    return report


def create_checkpoint(project: Project, *, origin: str | None = None,
                      signers: Sequence[Signer] = (), record: bool = True) -> tuple[Path,
                                                                                  Checkpoint]:
    """Write ``.air/checkpoints/<size>.checkpoint`` for the ledger as it is now.

    Refuses when the ledger is not an extension of the latest stored checkpoint (never
    sign two inconsistent checkpoints). With ``record`` the checkpoint is noted in the
    ledger itself (that entry is covered by the *next* checkpoint)."""
    previous = latest_checkpoint(project)
    if previous is not None:
        check = verify_against(project, previous[1])
        hard = [p for p in check.problems if not p.startswith("BAD SIGNATURE")]
        if hard:
            raise FrameworkError("refusing to checkpoint a ledger that is inconsistent with "
                                 "the latest checkpoint", previous=previous[0].name,
                                 problems=hard)
    chain = project.ledger.verify()
    if not chain.ok:
        raise FrameworkError("refusing to checkpoint a broken hash chain",
                             seq=chain.first_bad_seq, problem=chain.problem)
    tree = ledger_tree(project.ledger)
    ckpt = Checkpoint(origin or default_origin(project), len(tree), tree.root()).sign(
        *signers)
    d = checkpoint_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{ckpt.size:010d}.checkpoint"
    if path.exists() and Checkpoint.parse(path.read_text("utf-8")).root != ckpt.root:
        raise FrameworkError("a different checkpoint of this size exists", path=str(path))
    path.write_text(ckpt.to_text(), encoding="utf-8", newline="\n")
    if record:
        project.ledger.append("ledger.checkpointed", ckpt.origin, {
            "size": ckpt.size, "root": ckpt.root.hex(),
            "path": path.relative_to(project.root).as_posix(),
            "signed_by": [f"{s.name}/{s.key_id.hex()}" for s in ckpt.signatures]})
    return path, ckpt


def prove_entry(project: Project, seq: int, size: int | None = None) -> dict[str, Any]:
    """Inclusion proof for entry ``seq`` in the tree of ``size`` (default: all) entries,
    self-checked before it is returned."""
    entries = list(project.ledger)
    tree = MerkleTree([bytes.fromhex(e.hash) for e in entries])
    size = len(tree) if size is None else size
    if not 0 <= seq < size:
        raise SchemaError("no such entry in a tree of this size", seq=seq, size=size)
    proof = tree.inclusion_proof(seq, size)
    root = tree.root(size)
    entry = entries[seq]
    assert verify_inclusion(seq, size, tree.leaf(seq), proof, root)
    return {"seq": seq, "tree_size": size, "entry_hash": entry.hash,
            "entry_recomputed_ok": entry.compute_hash() == entry.hash,
            "leaf_hash": tree.leaf(seq).hex(), "root": root.hex(),
            "root_b64": base64.b64encode(root).decode(),
            "inclusion_proof": [p.hex() for p in proof],
            "algorithm": "RFC 9162 SHA-256; leaf input = ledger entry hash"}


def check_inclusion_document(doc: dict[str, Any]) -> bool:
    """Verify a :func:`prove_entry` document independently of the ledger."""
    return verify_inclusion(int(doc["seq"]), int(doc["tree_size"]),
                            leaf_hash(bytes.fromhex(doc["entry_hash"])),
                            [bytes.fromhex(p) for p in doc["inclusion_proof"]],
                            bytes.fromhex(doc["root"]))


# -- external anchors ----------------------------------------------------------------------
@dataclass(frozen=True)
class AnchorReceipt:
    anchor: str
    anchored: bool
    detail: str
    reference: str | None = None


class Anchor(Protocol):
    """Somewhere outside the project that witnesses a checkpoint."""

    name: str

    def anchor(self, path: Path, checkpoint: Checkpoint) -> AnchorReceipt: ...


Runner = Callable[[Sequence[str], Path], tuple[int, str]]


def _run(cmd: Sequence[str], cwd: Path) -> tuple[int, str]:
    try:
        out = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True, timeout=10,
                             check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return out.returncode, out.stdout.strip()


class GitAnchor:
    """Git history is the anchor: reports whether the checkpoint file is committed.
    It never commits by itself; committing is the owner's act."""

    name = "git"

    def __init__(self, root: Path, runner: Runner = _run) -> None:
        self.root = root
        self.runner = runner

    def anchor(self, path: Path, checkpoint: Checkpoint) -> AnchorReceipt:
        git = shutil.which("git") or "git"
        rel = path.resolve().relative_to(self.root.resolve()).as_posix()
        code, out = self.runner([git, "log", "-1", "--format=%H", "--", rel], self.root)
        if code == 0 and out:
            return AnchorReceipt("git", True, f"committed in {out[:12]}", out)
        return AnchorReceipt("git", False, f"not committed yet: git add {rel} && git commit")


class RekorAnchor:
    """Placeholder for a Sigstore Rekor (v2, tile-based) submission. Not implemented:
    publishing to a public log is irreversible and was left to the owner. A real
    implementation would submit the signed checkpoint (or a DSSE envelope over it) and
    store the returned inclusion proof next to the checkpoint."""

    name = "rekor"

    def __init__(self, url: str = "") -> None:
        self.url = url

    def anchor(self, path: Path, checkpoint: Checkpoint) -> AnchorReceipt:
        raise UnsupportedOperation("Rekor anchoring is not enabled (owner decision); "
                                   "commit the checkpoint to git instead", url=self.url)
