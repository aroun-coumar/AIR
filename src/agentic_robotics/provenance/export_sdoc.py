"""StrictDoc (``.sdoc``) export of the requirement matrix.

Why StrictDoc and not ReqIF: ReqIF is an XML exchange format whose interpretation
varies between tools (StrictDoc itself calls its ReqIF support experimental), and
writing an own ReqIF dialect is a known anti-pattern. StrictDoc's SDoc grammar is
small, public and checked by StrictDoc's own parser; StrictDoc then converts to ReqIF,
HTML, RST, Excel or JSON. So AIR writes SDoc, and anyone who needs ReqIF runs
``strictdoc export --formats reqif-sdoc``.

The document declares its own ``[GRAMMAR]`` so it does not depend on StrictDoc's default
grammar: one ``REQUIREMENT`` element with ``UID``, ``STATUS`` (the *claimed* status),
``TITLE``, ``STATEMENT``, AIR fields (kind, owner, verification method, evidence as
computed by the matrix) and ``Parent`` relations from ``traces_to``.

Checked against StrictDoc's grammar (``strictdoc/backend/sdoc/grammar/grammar.py`` and
``grammar_grammar.py``, 2026-10) and parsed with StrictDoc's reader in a scratch
environment; see ADR 0013.
"""

from __future__ import annotations

from collections.abc import Iterable

from .model import RequirementSet
from .traceability import MatrixRow

__requirements__ = ("F01", "REQ18")

FIELDS = (("UID", True), ("STATUS", False), ("TITLE", True), ("STATEMENT", False),
          ("AIR_KIND", False), ("AIR_OWNER", False), ("AIR_VERIFICATION", False),
          ("AIR_EVIDENCE", False), ("COMMENT", False))


def _single(text: str) -> str:
    return " ".join(text.split())


def _value(text: str) -> str | None:
    """SDoc value: single line when possible, otherwise a ``>>>``/``<<<`` block."""
    text = text.replace("[LINK: ", "[LINK : ").replace("[ANCHOR: ", "[ANCHOR : ")
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if not lines or not lines[0]:
        return None
    if len(lines) == 1 and not lines[0].startswith(">>>"):
        return lines[0]
    # A line starting with <<< would end the block; indent it by one space.
    body = "\n".join((" " + ln) if ln.startswith("<<<") else ln for ln in lines)
    return ">>>\n" + body + "\n<<<"


def to_sdoc(requirements: RequirementSet, rows: Iterable[MatrixRow] | None = None, *,
            title: str | None = None) -> str:
    by_id = {r.req_id: r for r in rows or ()}
    out = ["[DOCUMENT]", f"TITLE: {_single(title or requirements.name) or 'Requirements'}",
           "", "[GRAMMAR]", "ELEMENTS:", "- TAG: REQUIREMENT", "  FIELDS:"]
    for name, required in FIELDS:
        out += [f"  - TITLE: {name}", "    TYPE: String",
                f"    REQUIRED: {'True' if required else 'False'}"]
    out += ["  RELATIONS:", "  - TYPE: Parent"]
    for r in requirements.requirements:
        row = by_id.get(r.req_id)
        evidence = None
        if row is not None:
            evidence = (f"{row.computed}; {len(row.tests_passed)} passing, "
                        f"{len(row.tests_failed)} failing tests; {len(row.modules)} "
                        f"code locations; {row.activities} activities")
        values = {"UID": r.req_id, "STATUS": r.status.value, "TITLE": _single(r.title),
                  "STATEMENT": r.text, "AIR_KIND": r.kind.value, "AIR_OWNER": r.owner,
                  "AIR_VERIFICATION": ", ".join(r.verification), "AIR_EVIDENCE": evidence,
                  "COMMENT": None}
        out += ["", "[REQUIREMENT]"]
        for name, _ in FIELDS:
            raw = values.get(name)
            v = _value(raw) if raw else None
            if v is not None:
                out.append(f"{name}: {v}")
        if r.traces_to:
            out.append("RELATIONS:")
            for parent in r.traces_to:
                out += ["- TYPE: Parent", f"  VALUE: {parent}"]
    return "\n".join(out) + "\n"
