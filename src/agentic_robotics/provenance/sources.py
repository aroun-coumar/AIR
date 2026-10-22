"""Source catalog: open-source projects a project studies, and what it takes from each.

A research input is evidence like any other. Every entry records where the project
lives, its license (and whether AIR may reuse it under AGPL-3.0), how active it is,
what to learn from it, which AIR modules and requirements it informs, and which
fields were actually verified rather than recalled.

    {"$schema": "ar.source_catalog/1.0", "name": "AIR sources", "updated": "2026-10-22",
     "sources": [{"url": "https://github.com/huggingface/lerobot", "host": "github",
                  "owner": "huggingface", "name": "lerobot", "area": "robot_learning",
                  "license": "Apache-2.0", "agpl_compatible": true, "learn": "...",
                  "adopt_mode": "optional_integration", "priority": "P1",
                  "verified": ["license", "activity"]}]}

``refresh`` re-reads license, stars, activity and archive state from the host APIs
(GitHub and Gitee today) through an injectable fetcher, so tests stay offline.
"""

from __future__ import annotations

import dataclasses
import json
import os
import urllib.request
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from ..core.errors import SchemaError
from ..core.schema import Contract, contract

__requirements__ = ("REQ16", "REQ18", "AIR01")


class Host(StrEnum):
    GITHUB = "github"
    GITEE = "gitee"
    ATOMGIT = "atomgit"
    GITCODE = "gitcode"
    GITLAB = "gitlab"
    OTHER = "other"


class AdoptMode(StrEnum):
    ADOPT_PATTERN = "adopt_pattern"            # re-implement the idea in AIR
    OPTIONAL_INTEGRATION = "optional_integration"  # adapter / extra, never in the core
    REFERENCE = "reference"                    # read, cite, compare against
    AVOID = "avoid"                            # license, abandonment or design reasons


class Priority(StrEnum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"


class Verified(StrEnum):
    LICENSE = "license"
    ACTIVITY = "activity"
    FILES = "files"


#: SPDX ids AIR (AGPL-3.0-or-later) can depend on or reuse code from.
AGPL_COMPATIBLE = frozenset({
    "MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0", "ISC", "Zlib", "BSL-1.0",
    "MPL-2.0", "LGPL-2.1", "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0",
    "LGPL-3.0-only", "LGPL-3.0-or-later", "GPL-3.0", "GPL-3.0-only", "GPL-3.0-or-later",
    "AGPL-3.0", "AGPL-3.0-only", "AGPL-3.0-or-later", "GPL-2.0-or-later",
    "Unlicense", "CC0-1.0", "0BSD", "PSF-2.0", "Python-2.0", "BSD-3-Clause-Clear",
    "Artistic-2.0", "OFL-1.1",
})
#: Licenses that rule out reuse in AIR (proprietary terms, non-commercial, source-available).
AGPL_INCOMPATIBLE = frozenset({
    "GPL-2.0", "GPL-2.0-only", "BUSL-1.1", "SSPL-1.0", "CC-BY-NC-4.0", "CC-BY-NC-SA-4.0",
    "CC-BY-NC-ND-4.0", "NOSA", "Elastic-2.0", "none",
})


@dataclass(frozen=True)
class SourceRecord:
    url: str
    host: Host
    owner: str
    name: str
    area: str
    summary: str = ""
    topic: str = ""
    license: str | None = None
    agpl_compatible: bool | None = None
    stars: int | None = None
    last_push: str | None = None          # YYYY-MM-DD
    archived: bool | None = None
    language: str | None = None
    verified: tuple[Verified, ...] = ()
    files_read: tuple[str, ...] = ()
    learn: str = ""
    air_modules: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    adopt_mode: AdoptMode = AdoptMode.REFERENCE
    priority: Priority = Priority.P2
    notes: str = ""

    @property
    def key(self) -> str:
        return source_key(self.url)

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def source_key(url: str) -> str:
    """Normalized identity of a repository URL (scheme, case, ``.git`` and slashes ignored)."""
    u = url.strip().lower().split("://", 1)[-1].removeprefix("www.").rstrip("/")
    return u.removesuffix(".git")


@contract("source_catalog", version="1.0")
class SourceCatalog(Contract):
    name: str = "sources"
    updated: str | None = None
    sources: tuple[SourceRecord, ...] = ()

    def __post_init__(self) -> None:
        keys = [s.key for s in self.sources]
        dupes = sorted(k for k, n in Counter(keys).items() if n > 1)
        if dupes:
            raise SchemaError("duplicate sources in catalog", sources=dupes)

    def select(self, *, area: str | None = None, priority: Priority | None = None,
               module: str | None = None, requirement: str | None = None,
               adopt_mode: AdoptMode | None = None, text: str | None = None
               ) -> list[SourceRecord]:
        out = []
        for s in self.sources:
            if area and s.area != area:
                continue
            if priority and s.priority != priority:
                continue
            if module and module not in s.air_modules:
                continue
            if requirement and requirement not in s.requirements:
                continue
            if adopt_mode and s.adopt_mode != adopt_mode:
                continue
            if text and text.lower() not in " ".join(
                    (s.full_name, s.topic, s.summary, s.learn, s.notes)).lower():
                continue
            out.append(s)
        return sorted(out, key=lambda s: (s.priority.value, s.area, s.full_name.lower()))


def merge(records: Iterable[SourceRecord]) -> list[SourceRecord]:
    """Deduplicate by URL; for duplicates keep the better-verified record, union the links."""
    best: dict[str, SourceRecord] = {}
    for r in records:
        old = best.get(r.key)
        if old is None:
            best[r.key] = r
            continue
        keep, other = (r, old) if len(r.verified) > len(old.verified) else (old, r)
        best[r.key] = dataclasses.replace(
            keep,
            air_modules=tuple(dict.fromkeys(keep.air_modules + other.air_modules)),
            requirements=tuple(dict.fromkeys(keep.requirements + other.requirements)),
            files_read=tuple(dict.fromkeys(keep.files_read + other.files_read)),
            priority=min(keep.priority, other.priority, key=lambda p: p.value),
            notes="; ".join(n for n in (keep.notes, other.notes) if n),
        )
    return sorted(best.values(), key=lambda s: (s.area, s.full_name.lower()))


def license_compatibility(spdx: str | None) -> bool | None:
    if spdx is None or spdx == "NOASSERTION":
        return None
    if spdx in AGPL_COMPATIBLE:
        return True
    if spdx in AGPL_INCOMPATIBLE or "NC" in spdx.split("-"):
        return False
    return None


def problems(catalog: SourceCatalog, known_requirements: Iterable[str] = (),
             *, stale_after_days: int = 730, today: date | None = None) -> list[str]:
    """Consistency checks: license claims, unknown requirement ids, stale or archived adoption."""
    known = set(known_requirements)
    today = today or date.today()
    out: list[str] = []
    for s in catalog.sources:
        expected = license_compatibility(s.license)
        if expected is not None and s.agpl_compatible is not None \
                and expected != s.agpl_compatible:
            out.append(f"{s.full_name}: license {s.license} but agpl_compatible="
                       f"{s.agpl_compatible}")
        if s.adopt_mode is AdoptMode.OPTIONAL_INTEGRATION and s.agpl_compatible is False:
            out.append(f"{s.full_name}: optional integration with an AGPL-incompatible "
                       f"license ({s.license}); keep it a separate process or tool")
        if known:
            for req in s.requirements:
                if req not in known:
                    out.append(f"{s.full_name}: unknown requirement {req}")
        if s.adopt_mode is AdoptMode.OPTIONAL_INTEGRATION:  # patterns may come from old work
            if s.archived:
                out.append(f"{s.full_name}: archived but marked {s.adopt_mode.value}")
            if s.last_push and (today - date.fromisoformat(s.last_push)).days \
                    > stale_after_days:
                out.append(f"{s.full_name}: no push since {s.last_push}")
        if s.priority is Priority.P0 and not s.learn:
            out.append(f"{s.full_name}: P0 without a learning")
    return out


def stats(catalog: SourceCatalog) -> dict[str, Any]:
    src = catalog.sources
    return {
        "total": len(src),
        "by_area": dict(sorted(Counter(s.area for s in src).items())),
        "by_host": dict(sorted(Counter(s.host.value for s in src).items())),
        "by_priority": dict(sorted(Counter(s.priority.value for s in src).items())),
        "by_adopt_mode": dict(sorted(Counter(s.adopt_mode.value for s in src).items())),
        "license_verified": sum(Verified.LICENSE in s.verified for s in src),
        "files_read": sum(bool(s.files_read) for s in src),
        "agpl_compatible": {str(k).lower(): v for k, v in
                            Counter(s.agpl_compatible for s in src).items()},
        "by_module": dict(Counter(m for s in src for m in s.air_modules).most_common()),
        "by_requirement": dict(Counter(r for s in src for r in s.requirements)
                               .most_common()),
    }


# -- refresh ---------------------------------------------------------------------------
Fetcher = Callable[[str, dict[str, str]], Any]


def http_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "air-sources", **headers})
    with urllib.request.urlopen(req, timeout=20) as resp:  # fixed API hosts only
        return json.loads(resp.read().decode("utf-8"))


def _api(s: SourceRecord) -> tuple[str, dict[str, str]] | None:
    if s.host is Host.GITHUB:
        headers = {"Accept": "application/vnd.github+json"}
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return f"https://api.github.com/repos/{s.owner}/{s.name}", headers
    if s.host is Host.GITEE:
        return f"https://gitee.com/api/v5/repos/{s.owner}/{s.name}", {}
    return None


def refresh(record: SourceRecord, fetch: Fetcher = http_json) -> SourceRecord:
    """Re-read license, stars, last push and archive state from the host API."""
    api = _api(record)
    if api is None:
        return record
    data = fetch(*api)
    lic = data.get("license")
    spdx = (lic.get("spdx_id") or lic.get("key")) if isinstance(lic, dict) else lic
    spdx = spdx or "none"
    pushed = data.get("pushed_at") or data.get("updated_at")
    verified = set(record.verified) | {Verified.LICENSE, Verified.ACTIVITY}
    return dataclasses.replace(
        record,
        license=spdx,
        agpl_compatible=license_compatibility(spdx),
        stars=data.get("stargazers_count", record.stars),
        last_push=pushed[:10] if pushed else record.last_push,
        archived=bool(data.get("archived", False)),
        language=data.get("language") or record.language,
        verified=tuple(v for v in Verified if v in verified),
    )


# -- rendering -------------------------------------------------------------------------
def render_markdown(catalog: SourceCatalog) -> str:
    st = stats(catalog)
    lines = [
        f"# Source catalog: {catalog.name}",
        "",
        "Generated by `air sources render` from "
        "[`docs/research/catalog.json`](catalog.json) (`ar.source_catalog/1.0`). "
        "Do not edit by hand.",
        "",
        f"{st['total']} projects; license verified for {st['license_verified']}, "
        f"source files read for {st['files_read']}. Updated {catalog.updated or '-'}.",
        "",
        "| Area | Projects |", "|---|---|",
        *(f"| {a} | {n} |" for a, n in st["by_area"].items()),
        "",
        "Adoption modes: **adopt_pattern** re-implement the idea in AIR; "
        "**optional_integration** an adapter or extra outside the core; "
        "**reference** study and compare; **avoid** license or design reasons.",
        "",
    ]
    for area in st["by_area"]:
        lines += [f"## {area}", "",
                  "| P | Project | License | AGPL ok | Last push | Mode | What AIR takes |",
                  "|---|---|---|---|---|---|---|"]
        for s in catalog.select(area=area):
            ok = {True: "yes", False: "no", None: "?"}[s.agpl_compatible]
            learn = (s.learn or s.summary).replace("|", "\\|").replace("\n", " ")
            flag = " (archived)" if s.archived else ""
            lines.append(f"| {s.priority.value} | [{s.full_name}]({s.url}){flag} | "
                         f"{s.license or '?'} | {ok} | {s.last_push or '?'} | "
                         f"{s.adopt_mode.value} | {learn} |")
        lines.append("")
    return "\n".join(lines)
