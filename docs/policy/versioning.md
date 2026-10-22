# Versioning, stability and deprecation policy

Status: Accepted (2026-10-22). Applies to the `agentic-robotics` distribution, its public
Python API, its contracts and schemas, and the `air` CLI.

## Three independent version numbers

| What | Scheme | Where |
|---|---|---|
| The distribution | [SemVer](https://semver.org) (`MAJOR.MINOR.PATCH`, PEP 440 spelling on PyPI) | `pyproject.toml`, `agentic_robotics.__version__` |
| Each contract | `MAJOR.MINOR` per contract | `@contract("name", version=...)`, `$schema: ar.<name>/<ver>` |
| Ledger entry format | integer `v` | `.air/ledger.jsonl` (v1 legacy, v2 current) |

Contract versions move independently of the package. A package release can contain
contract minor bumps.

## Contracts and schemas

- **Minor** (`1.0` → `1.1`): only *adds* optional fields (with defaults) or enum values.
  Readers of `1.x` keep reading `1.y` documents of the same major.
- **Major** (`1.x` → `2.0`): anything else (removing, renaming or retyping a field,
  making a field required, removing an enum value, changing meaning). The strict parser
  rejects a different major.
- The JSON Schema for each published version lives in `schemas/ar.<name>/<ver>.json` and
  is **never edited** after publication. `air schema check` (run in CI) fails if:
  - a committed schema drifts from its contract, or
  - a minor is not a backwards-compatible superset of the previous minor.
- `$schema` may be the short tag `ar.<name>/<ver>` or its URI
  `https://raw.githubusercontent.com/aroun-coumar/AIR/main/schemas/ar.<name>/<ver>.json`.
  The URI form lets editors validate documents.

## Public API

- The public API is what each package's `__all__` exports, plus documented CLI commands
  and their flags. Anything prefixed `_`, and modules not re-exported, are internal.
- Stability markers:
  - **stable** (default)
  - **experimental**: `@experimental`; may change in any minor release without notice
  - **deprecated**: `@deprecated(...)`
- Contract fields flagged `experimental` in their contract (e.g. `SkillSpec.experimental`)
  follow the same rule.

## Before 1.0 (now)

- `0.MINOR` releases may break stable APIs, but each break gets a changelog "removal"
  entry and, where practical, one minor release of deprecation first.
- Patch releases never break stable APIs or contracts.

## From 1.0

- Stable APIs and contract majors break only in a new package major.
- Deprecations last at least one minor release before removal, and at least two where
  third-party adapters are affected (`plugins.py`, `conformance.py`, contract majors).

## How to deprecate

```python
from agentic_robotics.core.deprecation import deprecated

@deprecated(since="0.3.0", removal="0.5.0", alternative="Runtime.submit")
def old_submit(...): ...
```

- The warning is `AIRDeprecationWarning`, a subclass of `FutureWarning`, because
  `DeprecationWarning` is hidden by default.
- Every deprecation is registered. A test fails as soon as a symbol still exists at or
  after its `removal` version, so removals are not forgotten.
- Record the deprecation in the changelog and, when it changes behaviour or a contract,
  in an ADR.

## Releases

- Tags are `vMAJOR.MINOR.PATCH`. Release notes list breaking changes, deprecations,
  security fixes (advisories and CVEs), contract version changes, and schema files added.
- Every release is recorded as a `release` activity in the project ledger, with the
  built artifacts as generated entities.
- Planned (Wave P1): trusted publishing to PyPI with attestations and an SBOM.
