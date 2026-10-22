import json
from datetime import date

import pytest

from agentic_robotics.cli import main
from agentic_robotics.core.errors import SchemaError
from agentic_robotics.provenance import Project
from agentic_robotics.provenance.sources import (
    AdoptMode,
    Host,
    Priority,
    SourceCatalog,
    SourceRecord,
    Verified,
    license_compatibility,
    merge,
    problems,
    refresh,
    render_markdown,
    source_key,
    stats,
)

pytestmark = pytest.mark.req("REQ16", "REQ18")


def rec(name: str = "lerobot", owner: str = "huggingface", **kw: object) -> SourceRecord:
    base: dict[str, object] = dict(url=f"https://github.com/{owner}/{name}",
                                   host=Host.GITHUB, owner=owner, name=name,
                                   area="robot_learning")
    base.update(kw)
    return SourceRecord(**base)  # type: ignore[arg-type]


def test_source_key_normalizes_urls() -> None:
    assert source_key("https://GitHub.com/huggingface/lerobot.git/") == \
        source_key("http://www.github.com/huggingface/lerobot")


def test_catalog_round_trip_and_rejects_duplicates() -> None:
    cat = SourceCatalog(sources=(rec(license="Apache-2.0", verified=(Verified.LICENSE,)),))
    again = SourceCatalog.from_json(cat.to_json())
    assert again == cat
    with pytest.raises(SchemaError):
        SourceCatalog(sources=(rec(), rec(url="https://github.com/huggingface/lerobot.git")))
    with pytest.raises(SchemaError):  # strict: unknown fields are rejected
        SourceCatalog.from_dict({"$schema": "ar.source_catalog/1.0",
                                 "sources": [{**rec().__dict__, "bogus": 1}]})


def test_merge_keeps_better_verified_record_and_unions_links() -> None:
    a = rec(requirements=("REQ06",), priority=Priority.P2, notes="from A")
    b = rec(url="https://github.com/huggingface/lerobot/", requirements=("REQ09",),
            verified=(Verified.LICENSE, Verified.ACTIVITY), priority=Priority.P0,
            license="Apache-2.0", notes="from B")
    [m] = merge([a, b])
    assert m.license == "Apache-2.0" and m.priority is Priority.P0
    assert m.requirements == ("REQ09", "REQ06")
    assert "from A" in m.notes and "from B" in m.notes


def test_license_compatibility() -> None:
    assert license_compatibility("Apache-2.0") is True
    assert license_compatibility("GPL-2.0-only") is False
    assert license_compatibility("BUSL-1.1") is False
    assert license_compatibility("CC-BY-NC-4.0") is False
    assert license_compatibility("NOASSERTION") is None
    assert license_compatibility("MulanPSL-2.0") is None


def test_problems_flag_inconsistent_and_stale_entries() -> None:
    cat = SourceCatalog(sources=(
        rec("a", license="MIT", agpl_compatible=False),
        rec("b", license="BUSL-1.1", agpl_compatible=False,
            adopt_mode=AdoptMode.OPTIONAL_INTEGRATION),
        rec("c", requirements=("REQ99",)),
        rec("d", adopt_mode=AdoptMode.OPTIONAL_INTEGRATION, last_push="2020-01-01"),
        rec("e", adopt_mode=AdoptMode.OPTIONAL_INTEGRATION, archived=True),
        rec("f", priority=Priority.P0),
        rec("ok", license="MIT", agpl_compatible=True, learn="x", priority=Priority.P0),
    ))
    found = problems(cat, ["REQ06"], today=date(2026, 10, 22))
    joined = "\n".join(found)
    for name in ("huggingface/a:", "huggingface/b:", "REQ99", "huggingface/d:",
                 "huggingface/e:", "huggingface/f:"):
        assert name in joined
    assert "huggingface/ok" not in joined


def test_refresh_reads_host_metadata_offline() -> None:
    def fetch(url: str, headers: dict[str, str]) -> dict[str, object]:
        assert url == "https://api.github.com/repos/huggingface/lerobot"
        return {"license": {"spdx_id": "Apache-2.0"}, "stargazers_count": 21000,
                "pushed_at": "2026-10-20T08:00:00Z", "archived": False,
                "language": "Python"}

    r = refresh(rec(), fetch)
    assert (r.license, r.agpl_compatible, r.stars, r.last_push) == \
        ("Apache-2.0", True, 21000, "2026-10-20")
    assert set(r.verified) == {Verified.LICENSE, Verified.ACTIVITY}
    gitee = rec(host=Host.GITEE, url="https://gitee.com/x/y", owner="x", name="y")
    seen = []
    refresh(gitee, lambda u, h: seen.append(u) or {"license": None})
    assert seen == ["https://gitee.com/api/v5/repos/x/y"]
    other = rec(host=Host.OTHER)
    assert refresh(other, lambda u, h: pytest.fail("no API for other hosts")) is other


def test_select_stats_and_render() -> None:
    cat = SourceCatalog(name="t", sources=(
        rec("a", air_modules=("sandbox",), requirements=("REQ06",), priority=Priority.P0,
            learn="pattern | with pipe"),
        rec("b", area="sandbox_security", air_modules=("sandbox", "work")),
    ))
    assert [s.name for s in cat.select(module="sandbox")] == ["a", "b"]
    assert [s.name for s in cat.select(requirement="REQ06")] == ["a"]
    assert [s.name for s in cat.select(text="PIPE")] == ["a"]
    st = stats(cat)
    assert st["total"] == 2 and st["by_module"]["sandbox"] == 2
    md = render_markdown(cat)
    assert "## sandbox_security" in md and "pattern \\| with pipe" in md


def test_cli_merge_list_check_render_records_ledger(tmp_path, capsys) -> None:
    Project.init(tmp_path, name="t")
    lines = [json.dumps(rec("a", license="MIT", agpl_compatible=True).__dict__,
                        default=str),
             json.dumps({"url": "https://github.com/x/y", "nonsense": True}),
             ""]
    jsonl = tmp_path / "in.jsonl"
    jsonl.write_text("\n".join(lines), encoding="utf-8")
    proj = ["--project", str(tmp_path)]
    assert main(["sources", "merge", str(jsonl), *proj]) == 0
    assert main(["sources", "merge", str(jsonl), *proj, "--strict"]) == 1
    out = capsys.readouterr()
    assert "rejected" in out.err
    cat = SourceCatalog.load(tmp_path / "docs/research/catalog.json")
    assert [s.name for s in cat.sources] == ["a"]
    assert main(["sources", "list", *proj, "--area", "robot_learning"]) == 0
    assert "huggingface/a" in capsys.readouterr().out
    assert main(["sources", "check", *proj, "--strict"]) == 0
    assert main(["sources", "render", *proj]) == 0
    assert (tmp_path / "docs/research/catalog.md").exists()
    kinds = [e.kind for e in Project.open(tmp_path).ledger]
    assert kinds.count("sources.updated") == 2
