"""Ledger v2: RFC 8785 canonical hashing, mixed-version chains, serialized writers."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from agentic_robotics.provenance import Ledger
from agentic_robotics.provenance.canonical import canonical, es6_number, to_json_value
from agentic_robotics.provenance.ledger import LedgerLockTimeout, _file_lock

pytestmark = pytest.mark.req("REQ10", "F05")


@pytest.mark.parametrize(("value", "expected"), [
    (0.0, "0"), (-0.0, "0"), (1.0, "1"), (4.5, "4.5"), (-1.5, "-1.5"),
    (1e30, "1e+30"), (1e21, "1e+21"), (1e20, "100000000000000000000"),
    (0.000001, "0.000001"), (1e-7, "1e-7"), (123e-20, "1.23e-18"),
    (333333333.33333329, "333333333.3333333"), (5e-324, "5e-324"),
    (1.7976931348623157e308, "1.7976931348623157e+308"), (0.1, "0.1"),
])
def test_es6_number_formatting(value: float, expected: str) -> None:
    assert es6_number(value) == expected


def test_rfc8785_key_ordering_and_escaping() -> None:
    # Key order example from RFC 8785 section 3.2.3 (sorted by UTF-16 code units).
    obj = {"€": "Euro Sign", "\r": "Carriage Return", "דּ": "Hebrew Letter Dalet With Dagesh",
           "1": "One", "\U0001f600": "Emoji: Grinning Face", "\u0080": "Control",
           "ö": "Latin Small Letter O With Diaeresis"}
    keys = [k for k in json.loads(canonical(obj))]
    assert keys == ["\r", "1", "\u0080", "ö", "€", "\U0001f600", "דּ"]
    assert canonical({"b": [1, True, None], "a": "x\"\n\u001f"}) == \
        '{"a":"x\\"\\n\\u001f","b":[1,true,null]}'


def test_canonical_rejects_non_json_and_normalizer_is_deterministic() -> None:
    with pytest.raises(Exception, match="not plain JSON"):
        canonical({"p": Path("x")})
    assert to_json_value({"s": {3, 1, 2}, "t": (1, 2), "p": Path("a")}) == \
        {"s": [1, 2, 3], "t": [1, 2], "p": "a"}


def test_v2_hash_is_reproducible_independently(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl", wall_clock_ns=lambda: 1_700_000_000_123_456_789)
    e = ledger.append("thing", "s", {"x": 1.5, "y": [1, "a"]})
    raw = json.loads((tmp_path / "l.jsonl").read_text().splitlines()[0])
    assert raw["v"] == 2 and raw["ts_ns"] == 1_700_000_000_123_456_789
    body = {k: raw[k] for k in ("v", "seq", "ts_ns", "kind", "subject", "data", "trace_id",
                                "span_id", "prev")}
    # What any other implementation would do: JCS bytes, then SHA-256.
    jcs = ('{"data":{"x":1.5,"y":[1,"a"]},"kind":"thing","prev":"' + "0" * 64 +
           '","seq":0,"span_id":null,"subject":"s","trace_id":null,'
           '"ts_ns":1700000000123456789,"v":2}')
    assert canonical(body) == jcs
    assert hashlib.sha256(jcs.encode()).hexdigest() == e.hash == raw["hash"]


def test_v1_entries_still_verify_and_chain_into_v2(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    # A v1 entry exactly as the previous implementation wrote it.
    body = {"seq": 0, "ts": 1.5, "kind": "old", "subject": "s", "data": {"a": 1},
            "trace_id": None, "span_id": None, "prev": "0" * 64}
    h = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                  default=str).encode()).hexdigest()
    path.write_text(json.dumps({**body, "hash": h}) + "\n")
    ledger = Ledger(path)
    new = ledger.append("new", "s")
    assert new.v == 2 and new.prev == h
    assert Ledger(path).verify().ok
    lines = path.read_text().splitlines()
    tampered = json.loads(lines[1])
    tampered["data"] = {"injected": True}
    path.write_text(lines[0] + "\n" + json.dumps(tampered) + "\n")
    report = Ledger(path).verify()
    assert not report.ok and report.first_bad_seq == 1


def test_concurrent_writer_processes_extend_one_chain(tmp_path: Path) -> None:
    path = tmp_path / "shared.jsonl"
    # The property under test is chain integrity, not lock latency: a long lock timeout
    # keeps a heavily loaded machine from failing a writer that is merely waiting.
    script = ("import sys; from agentic_robotics.provenance import Ledger\n"
              "l = Ledger(sys.argv[1], lock_timeout_s=60.0)\n"
              "for i in range(25):\n    l.append('k', sys.argv[2] + '-' + str(i))\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(path), f"w{n}"])
             for n in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    final = Ledger(path)
    assert len(final) == 100
    report = final.verify()
    assert report.ok, report


def test_writer_refuses_when_lock_is_held(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path / "l.jsonl", lock_timeout_s=0.1)
    assert ledger.lock_path is not None
    with _file_lock(ledger.lock_path, 1.0), pytest.raises(LedgerLockTimeout):
        ledger.append("k", "blocked")
    ledger.append("k", "after")  # lock released: appends again
    assert len(ledger) == 1
