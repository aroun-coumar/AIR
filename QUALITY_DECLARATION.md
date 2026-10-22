# Quality declaration: `agentic-robotics`

This document declares the quality level of the `agentic-robotics` package (import
`agentic_robotics`) against [ROS REP 2004](https://www.ros.org/reps/rep-2004.html)
"Package Quality Categories". AIR is not a ROS package; REP 2004 is used because it is the
most concrete public checklist for robotics software quality.

**Target: Quality Level 2. Current status: Quality Level 4 (pre-release).** The package is
at `0.1.0.dev0`, so it cannot claim Level 1 or 2 version stability yet. Each requirement
below states honestly whether it is met today. Updated 2026-10-22.

Legend: **met**, **partial**, **not met**.

## 1. Version policy

| Requirement | Status | Evidence |
|---|---|---|
| 1.i A version policy | met | SemVer for the distribution, per-contract versions; [docs/policy/versioning.md](docs/policy/versioning.md) |
| 1.ii A stable version (>= 1.0.0) | **not met** | `0.1.0.dev0`, classifier "Pre-Alpha" |
| 1.iii A strictly declared public API | partial | `agentic_robotics.__all__`, contracts and published JSON Schemas under `schemas/`; integrations are optional and pre-1.0 |
| 1.iv API stability policy | met | versioning policy, `AIRDeprecationWarning`, `@deprecated`/`@experimental` |
| 1.v ABI stability policy | met (n/a) | pure Python; the wire/ABI surface is the JSON Schemas, checked by `air schema check` (minor is a superset) |
| 1.vi Stability within a release line | partial | stated in the policy; no release line exists yet |

## 2. Change control

| Requirement | Status | Evidence |
|---|---|---|
| 2.i Changes through change requests | partial | PR template and CODEOWNERS exist; direct pushes to `main` are not yet blocked by branch protection |
| 2.ii Contributor origin (DCO/CLA) | **not met** | no DCO or CLA yet; this is tied to the open licence decision (ADR 0004) |
| 2.iii CI for all change requests | met | `.github/workflows/ci.yml` on every PR: lint, strict types, schema check, tests with requirement evidence, traceability and ledger integrity on Linux and Windows, Python 3.11 and 3.12 |
| (L1 only) peer review policy | partial | CODEOWNERS review is requested; not enforced |
| (L1 only) documentation policy for changes | partial | PR checklist and news fragments (`changelog.d/`, checked by `.github/workflows/news.yml`) |

## 3. Documentation

| Requirement | Status | Evidence |
|---|---|---|
| 3.i Documentation for each feature | partial | README, `docs/architecture.md`, ADRs, `AGENTS.md` per package, docstrings; no rendered user guide yet |
| 3.ii A declared license | met | AGPL-3.0-or-later (`LICENSE`, `pyproject.toml`); the decision is open with the owner (ADR 0004) |
| 3.iii Copyrights and authors stated | partial | `pyproject.toml` authors; per-file copyright headers are not used; third-party sources are cited in ADRs with their licenses |
| 3.iv This quality declaration | met | this file |
| 3.iv.a Linked from the README | **not met** | to be linked when the README is next revised (the README is owned by the lead) |
| 3.iv.b/c Registered with a Level-N list | not met | no such list exists for non-ROS packages |

## 4. Testing

| Requirement | Status | Evidence |
|---|---|---|
| 4.i System tests covering the features | partial | 400+ offline tests; adapter conformance suite (`air conformance`); bridges are tested against fake peers that speak the verified wire protocols, **not** against real robots or simulators |
| 4.ii.a Code coverage tracking | **not met** | requirement-level evidence is tracked (`air requirements matrix --check`), line coverage is not |
| 4.iii.a Code style enforced | met | `ruff` in CI |
| 4.iii.b Static analysis | met | `mypy --strict` in CI |

## 5. Dependencies

| Requirement | Status | Evidence |
|---|---|---|
| 5.i/ii No runtime dependencies below the level | met | the core has **no** third-party runtime dependencies |
| 5.iii Justified non-ROS dependencies | partial | optional extras only (`anthropic`, `piper`, `vda5050`, `openpi`, `dora`, `lerobot`); they are not assessed and are never imported by the core |

## 6. Platform support

| Requirement | Status | Evidence |
|---|---|---|
| 6.i All target platforms | partial | CI on Ubuntu and Windows, CPython 3.11/3.12; macOS is not in CI. Device bridges assume Linux on the robot side (unix sockets, SocketCAN) |

## 7. Security

| Requirement | Status | Evidence |
|---|---|---|
| 7.i Vulnerability disclosure policy and schedule | met | [SECURITY.md](SECURITY.md): acknowledgement within 14 days, assessment within 30 |

## What is needed for Level 2

1. Release 1.0.0 with a declared public API (1.ii, 1.iii).
2. Decide the licence and add a DCO or CLA (2.ii).
3. Enforce branch protection and review (2.i).
4. Add line-coverage tracking in CI (4.ii.a).
5. Publish user documentation per feature and link this declaration from the README
   (3.i, 3.iv.a).
6. Add macOS to CI (6.i).

AIR claims no safety-standard compliance. Bridges never imply protection that a robot
lacks (for example, Microduck has no per-joint or thermal software limits).
