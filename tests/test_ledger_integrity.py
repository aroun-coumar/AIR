"""RFC 9162 Merkle proofs, C2SP checkpoints and signing over the project ledger."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from agentic_robotics.cli import main
from agentic_robotics.core.errors import FrameworkError, SchemaError, UnsupportedOperation
from agentic_robotics.provenance import Ledger, Project
from agentic_robotics.provenance.merkle import (
    Checkpoint,
    GitAnchor,
    MerkleTree,
    RekorAnchor,
    check_inclusion_document,
    create_checkpoint,
    latest_checkpoint,
    prove_entry,
    verify_against,
    verify_consistency,
    verify_inclusion,
)
from agentic_robotics.provenance.signing import (
    Ed25519PublicKey,
    Ed25519Signer,
    HmacKey,
    SigningUnavailable,
    ed25519_verify,
)

from .ed25519_ref import RefSigner, public_key

pytestmark = pytest.mark.req("REQ10", "REQ16")

# RFC 6962 / certificate-transparency reference leaves and roots.
CT_LEAVES = [b"", b"\x00", b"\x10", b"\x20\x21", b"\x30\x31", b"\x40\x41\x42\x43",
             bytes(range(0x50, 0x58)), bytes(range(0x60, 0x70))]
CT_ROOTS = [
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]
HMAC_SECRET = b"0123456789abcdef0123456789abcdef"


@pytest.fixture
def project(tmp_path: Path) -> Project:
    p = Project.init(tmp_path / "proj", name="demo")
    for i in range(6):
        p.ledger.append("thing", f"s{i}", {"i": i})
    return p


def test_tree_roots_match_the_rfc6962_reference_vectors() -> None:
    tree = MerkleTree(CT_LEAVES)
    assert [tree.root(n).hex() for n in range(1, 9)] == CT_ROOTS
    assert MerkleTree([]).root().hex() == \
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_inclusion_and_consistency_proofs_verify_and_reject_tampering() -> None:
    for n in range(1, 34):
        tree = MerkleTree([bytes([i]) * 3 for i in range(n)])
        root = tree.root(n)
        for i in range(n):
            proof = tree.inclusion_proof(i, n)
            assert verify_inclusion(i, n, tree.leaf(i), proof, root)
            if n > 1:
                assert not verify_inclusion(i, n, tree.leaf((i + 1) % n), proof, root)
        for m in range(0, n + 1):
            proof = tree.consistency_proof(m, n)
            assert verify_consistency(m, n, tree.root(m), root, proof)
            if 0 < m < n:
                assert not verify_consistency(m, n, tree.root(m), b"\x00" * 32, proof)
                assert not verify_consistency(m, n, b"\x01" * 32, root, proof)


def test_checkpoint_text_is_the_c2sp_format_and_round_trips() -> None:
    ckpt = Checkpoint("example.com/log", 20852163, bytes(range(32)))
    assert ckpt.to_text() == ("example.com/log\n20852163\n"
                              + base64.b64encode(bytes(range(32))).decode() + "\n")
    key = HmacKey("example.com/log", HMAC_SECRET)
    signed = ckpt.sign(key)
    text = signed.to_text()
    _body, _, sigs = text.partition("\n\n")
    assert sigs.startswith("— example.com/log ") and text.endswith("\n")
    back = Checkpoint.parse(text)
    assert back == signed and back.check_signatures([key]).ok
    with pytest.raises(SchemaError, match="base64"):
        Checkpoint.parse(ckpt.to_text().replace("=", ""))
    with pytest.raises(SchemaError, match="extension"):
        Checkpoint.parse(ckpt.to_text() + "extra line\n")
    with pytest.raises(SchemaError, match="canonical decimal"):
        Checkpoint.parse(ckpt.to_text().replace("20852163", "020852163"))


def test_c2sp_signed_note_example_verifies_with_the_stdlib_ed25519_verifier() -> None:
    # The example verifier key and signed note from c2sp.org/signed-note.
    vkey = Ed25519PublicKey.from_vkey(
        "example.com/foo+530d903a+AekyeRrm56hApGFkyQR4ZCbV54Id2LKaANYcrnKv3U2k")
    raw = base64.b64decode("Uw2QOkn8srV1yJGh2VYRlL1Tnagv1YEq6TfXppzi2ONncAlTgK7Ztg1ERYNZXsYj"
                           "OBH3mFXmRKuwHjG1Yu72IneyaQM=")
    assert raw[:4] == vkey.key_id
    assert vkey.verify(b"This is an example message.\n", raw[4:])
    assert not vkey.verify(b"This is an example message!\n", raw[4:])
    # RFC 8032 section 7.1 test 1
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
                        "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    assert public_key(seed) == pub and ed25519_verify(pub, b"", sig)
    assert not ed25519_verify(pub, b"x", sig)


def test_ed25519_signed_checkpoint_and_signature_reports() -> None:
    signer = RefSigner("air.local/demo", bytes(range(32)))
    ckpt = Checkpoint("air.local/demo", 3, b"\x07" * 32).sign(signer)
    assert Ed25519PublicKey.from_vkey(signer.public.vkey()) == signer.public
    assert ckpt.check_signatures([signer.public]).ok
    assert "verified" in ckpt.check_signatures([signer.public]).describe()
    unchecked = ckpt.check_signatures([])
    assert not unchecked.ok and "NOT checked" in unchecked.describe()
    forged = Checkpoint(ckpt.origin, 4, ckpt.root, ckpt.signatures)
    assert forged.check_signatures([signer.public]).failed == ("air.local/demo",)
    assert "UNSIGNED" in Checkpoint("o", 0, b"\x00" * 32).check_signatures([]).describe()


def test_ed25519_signing_needs_the_optional_extra_or_works_with_it() -> None:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        with pytest.raises(SigningUnavailable, match=r"\[signing\]"):
            Ed25519Signer("k", b"\x01" * 32)
        return
    s = Ed25519Signer("k", bytes(range(32)))
    assert s.public.public == public_key(bytes(range(32)))
    assert ed25519_verify(s.public.public, b"m", s.sign(b"m"))


def test_project_checkpoint_detects_truncation_and_rewrite(project: Project) -> None:
    path, ckpt = create_checkpoint(project, signers=[HmacKey("air.local/demo", HMAC_SECRET)])
    assert path.parent.name == "checkpoints" and ckpt.size == 7  # 1 created + 6 things
    assert project.ledger.query(kind="ledger.checkpointed")[-1].data["size"] == 7
    project.ledger.append("thing", "later", {})
    report = verify_against(project, ckpt, [HmacKey("air.local/demo", HMAC_SECRET)])
    assert report.ok, report.problems
    assert report.consistency_proof and "verified" in report.signature

    lines = project.ledger.path.read_text().splitlines()  # type: ignore[union-attr]
    project.ledger.path.write_text("\n".join(lines[:5]) + "\n")  # type: ignore[union-attr]
    truncated = verify_against(Project.open(project.root), ckpt)
    assert not truncated.ok and any("truncated" in p for p in truncated.problems)
    assert Project.open(project.root).ledger.verify().ok  # the chain alone cannot tell

    # A complete, internally consistent rewrite of the same length.
    project.ledger.path.unlink()  # type: ignore[union-attr]
    fresh = Ledger(project.ledger.path)
    for i in range(9):
        fresh.append("forged", f"f{i}", {})
    rewritten = verify_against(Project.open(project.root), ckpt)
    assert fresh.verify().ok and not rewritten.ok
    assert any("rewritten" in p for p in rewritten.problems)
    with pytest.raises(FrameworkError, match="inconsistent"):
        create_checkpoint(Project.open(project.root))


def test_inclusion_proof_document_is_checkable_without_the_ledger(project: Project) -> None:
    doc = prove_entry(project, 3)
    assert doc["entry_recomputed_ok"] and check_inclusion_document(doc)
    tampered = {**doc, "entry_hash": "00" * 32}
    assert not check_inclusion_document(tampered)
    with pytest.raises(SchemaError):
        prove_entry(project, 99)


def test_anchors_git_reports_and_rekor_refuses(project: Project) -> None:
    path, ckpt = create_checkpoint(project, record=False)
    calls: list[list[str]] = []

    def runner(cmd: list[str], cwd: Path) -> tuple[int, str]:
        calls.append(list(cmd))
        return (0, "abc123def4567890") if len(calls) > 1 else (0, "")

    anchor = GitAnchor(project.root, runner)  # type: ignore[arg-type]
    assert not anchor.anchor(path, ckpt).anchored
    receipt = anchor.anchor(path, ckpt)
    assert receipt.anchored and receipt.reference == "abc123def4567890"
    assert calls[0][1:3] == ["log", "-1"]
    with pytest.raises(UnsupportedOperation, match="owner decision"):
        RekorAnchor("https://rekor.example").anchor(path, ckpt)


def test_cli_checkpoint_prove_verify_since_and_keygen(
        project: Project, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    key = tmp_path / "keys" / "ckpt.hmac"
    key.parent.mkdir()
    root = str(project.root)
    assert main(["ledger", "keygen", "--type", "hmac", "--out", str(key),
                 "--project", root]) == 0
    assert main(["ledger", "checkpoint", "--project", root, "--hmac-key", str(key)]) == 0
    out = capsys.readouterr().out
    assert "signed by air.local/demo (hmac-sha256" in out and "anchor:" in out
    assert main(["ledger", "verify", "--project", root, "--since", "latest",
                 "--hmac-key", str(key)]) == 0
    out = capsys.readouterr().out
    assert "ledger OK against checkpoint" in out and "signature verified" in out
    assert main(["ledger", "verify", "--project", root]) == 0  # plain chain check still works
    assert "ledger OK:" in capsys.readouterr().out
    assert main(["ledger", "prove", "2", "--project", root, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert check_inclusion_document(doc)
    latest = latest_checkpoint(project)
    assert latest is not None
    # A second checkpoint extends the first.
    assert main(["ledger", "checkpoint", "--project", root]) == 0
    assert "UNSIGNED" in capsys.readouterr().out
    assert main(["ledger", "verify", "--project", root, "--since", str(latest[0])]) == 0
    assert "NOT checked" in capsys.readouterr().out
