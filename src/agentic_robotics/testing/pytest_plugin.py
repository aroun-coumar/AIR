"""pytest plugin: turns test results into requirement evidence.

Loaded automatically when the framework is installed (``pytest11`` entry point).
It only registers the ``req`` marker unless asked to do more:

    pytest --air-evidence .air/test-evidence.json      # write evidence file
    pytest --air-evidence ... --air-record             # also log an evaluation activity
    pytest --air-evidence ... --air-attest run.intoto.json   # in-toto test-result statement

Mark tests with ``@pytest.mark.req("REQ09", "G06")``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

__requirements__ = ("REQ02", "REQ18", "REQ10")


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("air", "AIR requirements evidence")
    group.addoption("--air-evidence", default=None, metavar="PATH",
                    help="write requirement evidence (JSON) for this run to PATH")
    group.addoption("--air-record", action="store_true", default=False,
                    help="also record this run as an evaluation activity in the project "
                         "ledger (requires an initialized .air project)")
    group.addoption("--air-attest", default=None, metavar="PATH",
                    help="also write an in-toto test-result/v0.1 statement for this run "
                         "(needs --air-evidence)")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "req(*ids): requirements or gates this test provides evidence for")
    path = config.getoption("--air-evidence")
    attest = config.getoption("--air-attest")
    if attest and not path:
        raise pytest.UsageError("--air-attest needs --air-evidence")
    if path:
        config.pluginmanager.register(
            EvidenceCollector(Path(path), bool(config.getoption("--air-record")),
                              Path(str(config.rootpath)),
                              Path(attest) if attest else None), "air-evidence")


class EvidenceCollector:
    def __init__(self, path: Path, record: bool, root: Path,
                 attest: Path | None = None) -> None:
        self.path = path
        self.attest = attest
        self.record = record
        self.root = root
        self.started = time.time()
        self.reqs: dict[str, list[str]] = {}
        self.outcomes: dict[str, str] = {}
        self.durations: dict[str, float] = {}

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_makereport(self, item: pytest.Item,
                                  call: pytest.CallInfo[None]) -> Generator[None, Any, None]:
        outcome = yield
        report = outcome.get_result()
        ids: list[str] = []
        for mark in item.iter_markers("req"):
            ids.extend(str(a) for a in mark.args)
        self.reqs[item.nodeid] = sorted(set(ids))
        if report.when == "setup" and report.failed:
            self.outcomes[item.nodeid] = "error"
        elif report.when == "setup" and report.skipped:
            self.outcomes[item.nodeid] = "skipped"
        elif report.when == "call":
            self.outcomes[item.nodeid] = report.outcome
            self.durations[item.nodeid] = round(report.duration, 4)
        elif report.when == "teardown" and report.failed:
            self.outcomes[item.nodeid] = "error"

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        from ..provenance.project import capture_environment

        tests: list[dict[str, Any]] = [
            {"nodeid": n, "outcome": o, "requirements": self.reqs.get(n, []),
             "duration_s": self.durations.get(n)}
            for n, o in sorted(self.outcomes.items())]
        totals: dict[str, int] = {}
        for t in tests:
            totals[t["outcome"]] = totals.get(t["outcome"], 0) + 1
        doc = {"run": {"started_at": self.started, "finished_at": time.time(),
                       "exitstatus": int(exitstatus), "totals": totals,
                       "environment": capture_environment(self.root),
                       "requirement_fingerprints": self._fingerprints(tests)},
               "tests": tests}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
        if self.attest is not None:
            from ..provenance.attest import evidence_statement

            statement = evidence_statement(doc, root=self.root)
            self.attest.parent.mkdir(parents=True, exist_ok=True)
            self.attest.write_text(json.dumps(statement, indent=2, sort_keys=True) + "\n",
                                   encoding="utf-8", newline="\n")
        if self.record:
            self._record(doc)

    def _fingerprints(self, tests: list[dict[str, Any]]) -> dict[str, str]:
        """Fingerprints of the cited requirements as they read *now*, so later changes
        to a requirement mark this evidence stale. Empty outside an AIR project."""
        from ..core.errors import FrameworkError
        from ..provenance.project import Project
        from ..provenance.traceability import requirement_fingerprints

        try:
            reqs = Project.open(self.root).requirements
        except FrameworkError:
            return {}
        if reqs is None:
            return {}
        cited = {r for t in tests for r in t["requirements"]}
        return requirement_fingerprints(reqs, cited)

    def _record(self, doc: dict[str, Any]) -> None:
        from ..provenance.project import Project

        project = Project.open(self.root)
        cited = sorted({r for t in doc["tests"] for r in t["requirements"]})
        known = project.requirements
        if known is not None:
            cited = [r for r in cited if known.get(r) is not None]
        with project.activity("evaluation", "automated test run", requirements=cited,
                              actors=("pytest",)) as act:
            evidence = act.generate(self.path, kind="test_evidence")
            if self.attest is not None:
                act.generate(self.attest, kind="attestation", derived_from=[evidence])
            for k, v in doc["run"]["totals"].items():
                act.metric(k, v)
            failed = doc["run"]["totals"].get("failed", 0) + doc["run"]["totals"].get("error", 0)
            if failed:
                act.fail(f"{failed} test(s) failed")
            act.outcome = "all passed" if not failed else f"{failed} failed"
