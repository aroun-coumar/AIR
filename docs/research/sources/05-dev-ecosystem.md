# Research source 05: developer ecosystem, project hygiene and safety practice

Raw findings from the 2026-10-22 survey (researcher report, lightly edited). Claims
marked *unverified* were not confirmed against a primary source. The synthesis and
priorities are in [../oss-adoption-plan.md](../oss-adoption-plan.md).

## Gaps found in AIR

- No `SECURITY.md`, `CODEOWNERS`, `CHANGELOG`, `CITATION.cff`, `.pre-commit-config.yaml`,
  issue/PR templates, `dependabot.yml`, docs site or release workflow.
- `ci.yml` has no `permissions:` block, and its actions are pinned by tag, not SHA. Scorecard
  fails both Token-Permissions and Pinned-Dependencies on this.
- `LocalRegistry.fetch` checks the digest against the digest stored in the same file. That
  detects corruption, not tampering. `AssetRef.digest` is never used to pin a fetch.
- `_version_key("1.0.0-rc1")` sorts after `"1.0.0"`, so "latest" can be a prerelease.
- `ConformanceReport` records no AIR, suite or contract versions, so it cannot back a badge.
- `$schema` in JSON tooling normally names a schema URI. `"ar.observation/1.0"` is not a URI
  (an editor-behaviour risk; *unverified*).

## Sources read

| Project | Evidence | License | Mechanism |
|---|---|---|---|
| transformers | docs: philosophy, model_doc/auto; `src/transformers/utils/deprecation.py` | Apache-2.0 | config/model/preprocessor triple; `from_pretrained`/`save_pretrained`; Auto* resolve by `model_type`; `register()` checks the key and class match; `trust_remote_code=False` by default; `deprecate_kwarg` emits `FutureWarning`, because `DeprecationWarning` is hidden by default |
| huggingface_hub | hub/model-cards, guides/download, security-pickle | Apache-2.0 | model/dataset/space repos; cards are README.md with YAML (license, base_model plus relation, new_version, model-index evals); revisions pin a commit hash; pickle scanning is "not foolproof" |
| safetensors | docs | Apache-2.0 | data-only weights, no code execution on load |
| LangChain | `libs/README.md`, `libs/core/langchain_core/_api/deprecation.py` | MIT | monorepo with `core`, `partners/`, `standard-tests/`; most integrations in their own repos; `@deprecated(since, removal, alternative, pending)` |
| pluggy | readthedocs | MIT | hookspec/hookimpl, entry-point loading, `firstresult`/`historic`/`wrapper`; implementations may take a subset of arguments |
| Pydantic / msgspec | JSON Schema docs | MIT / BSD-3 | Draft 2020-12 export; stdlib dataclass support *unverified* |
| ROS REP 2004 | `ros-infrastructure/rep/rep-2004.rst` | - | Quality Levels 1-5: version policy, change control, docs and a Quality Declaration, testing, dependencies, platforms, vulnerability disclosure |
| launch_testing | `ros2/launch/launch_testing/README.md` | Apache-2.0 | `ReadyToTest`, active tests, `@post_shutdown_test`, `assertWaitFor` |
| SROS2 | `ros2/sros2` README, `SROS2_Linux.md` | Apache-2.0 | keystore/enclave/permissions from policy; `ROS_SECURITY_STRATEGY=Enforce` |
| Autoware | docs.autoware.org contributing | Apache-2.0 | coding, testing and PR guidelines, code owners, an AI contribution policy |
| OpenSSF Scorecard | `ossf/scorecard/docs/checks.md` | Apache-2.0 | 20 checks; High: Branch-Protection, Code-Review, Dependency-Update-Tool, Signed-Releases, Token-Permissions, Vulnerabilities |
| OpenSSF Best Practices | bestpractices.dev criteria | - | vulnerability reporting with a 14-day response, automated tests, release notes |
| PyPI Trusted Publishing | docs.pypi.org | - | OIDC with short-lived tokens; PEP 740 attestations by default |
| towncrier / copier | docs | MIT | news fragments; `copier update` re-applies template changes onto generated projects |
| LeRobot | `pyproject.toml` | Apache-2.0 | one package, about 60 extras, many console scripts |
| Viam RDK / Ultralytics | LICENSE / README | AGPL-3.0 | Ultralytics pairs AGPL with a paid enterprise license |
| Isaac Lab / MoveIt 2 | license pages | BSD-3-Clause | - |
| openEuler SIG-ROS | `sig/sig-ROS/sig-info.yaml` | - | machine-readable governance: maintainers, repos, committers, maturity |
| MindSpore / PaddlePaddle | community READMEs | Apache-2.0 | TSC, SIGs, security folder, ICLA/CCLA; Paddle `rfcs/` |
| OpenHarmony | AtomGit SIG README | - | *unreachable* (login wall) |

## Decisions recommended

- **(a) JSON Schema for every contract (P0, M).** Stdlib generator from the dataclass hints;
  publish `schemas/ar.<name>/<ver>.json`; CI drift check plus a minor-is-superset
  compatibility check; property tests that schema and parser agree; settle the `$schema`
  URI-vs-tag question before 1.0.
- **(b) Package split (P2 now; P1 at the first heavy integration).** Keep `agentic-robotics`
  as the core. Integrations become `agentic-robotics-<x>`, and conformance becomes the
  shared standard test suite. Avoid a "community" mega-package.
- **(c) Asset cards (P1, M).** A strict `ar.asset_card/1.0` covering license (SPDX),
  derived_from, superseded_by, intended and out-of-scope use, operating envelope, hazards,
  evaluations (scenario digest, metric, seed, AIR version), conformance digest,
  requirements and maintainers. Cards are claims, not authority.
- **(d) Plugin template with copier, and a conformance badge (P1, M).** Issue the badge
  only from a versioned `ar.conformance_report/1.0`.
- **(e) Release, changelog and deprecation policy (P0 doc, P1 tooling).** SemVer for the
  distribution; declared public API; `AIRDeprecationWarning(FutureWarning)`, `@deprecated`
  and `@experimental`; towncrier.
- **(f) Supply chain (P0, S each).**
  - CI `permissions: contents: read`, SHA-pinned actions and Dependabot.
  - A trusted-publishing release workflow with attestations and an SBOM.
  - Scorecard, SAST (ruff `S`), and hypothesis fuzzing of `from_dict`.
  - `SECURITY.md`.
  - Registry `fetch(..., digest=)` and an `air.lock`; fix prerelease ordering.
  - Never load code or pickle from assets.
- **(g) Docs site (P1, M).** MkDocs Material with mkdocstrings, organized Diátaxis-style
  (tutorials, how-to, concepts, reference with generated schema and CLI pages, policies,
  decisions); `mkdocs build --strict`.

Other items:
- verify `cls.kind == kind` in `register_adapter`
- `Robot.from_registry`
- pluggy-style lifecycle hooks, reimplemented in the stdlib
- a Quality Declaration targeting REP 2004 QL2
- a launch_testing-style scenario harness
- SROS2-style transport authentication (no home-made crypto)
- CODEOWNERS and templates
- pre-commit
- `CITATION.cff`
- an AI contribution policy
- DCO
- governance YAML
- RFCs
- a tool-qualification note that explicitly does **not** claim IEC 61508 / ISO 26262 compliance

## License observation

The libraries AIR wants to resemble are permissive: transformers, huggingface_hub,
LeRobot, MindSpore, Paddle and Autoware are Apache-2.0; LangChain is MIT; Isaac Lab and
MoveIt are BSD. AGPL examples are Viam RDK and Ultralytics, the latter with a commercial
license. AGPL section 13 is likely to deter product embedding and hosted use. Decide
before the first PyPI release, because relicensing later needs every contributor's consent.
At minimum, license the schemas and the conformance suite permissively. How AGPL applies to
plugins is a legal question for counsel (*unverified*).

## Anti-patterns

- remote code or pickle loaded from assets
- a mega-package of integrations
- self-asserted badges
- hidden `DeprecationWarning`s
- the core depending on a schema library
- loose YAML metadata
- tag-pinned actions and default write tokens in CI
- "latest" resolving to a prerelease
- claiming safety-standard compliance
- splitting packages too early
