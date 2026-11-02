# 0020. Package library: interfaces, packages, compatibility, services, skill promotion

Status: Accepted
Date: 2026-11-02

## Context
The survey of similar projects (2026-10-23) found two peers ahead of AIR in reusable,
compatibility-checked skills and services:
- **syswonder/robonix** (MulanPSL-2.0): versioned capability contracts that callers bind
  to; packages that provide them; a registry of running providers (Atlas) with
  heartbeat and lifecycle; a body file that maps parts to the capabilities they export;
  a published package catalog.
- **ros-claw/rosclaw** (MIT): skill packages that declare robot compatibility, provider
  routes, safety and evaluation; staged promotion bound to lineage and package hashes.

AIR had a flat `SkillRegistry` and free-text required capabilities. The owner asked to
learn from what these projects already implement.

What their code shows, read directly:
- **robonix**
  - Contract TOMLs carry `id`, `version`, `kind`, `idl` and `mode`. Packages list the
    contract ids they provide.
  - Atlas tracks registration, heartbeat and five lifecycle states. It has **no
    authentication**: any client that reaches its port can register.
  - The published catalog has **no structured requires, version ranges or robot
    compatibility**. Compatibility is shared contract ids, and versions are free-form
    strings.
- **rosclaw**
  - Skill packages: `e-urdf-compat.yaml` lists compatible robots with required limbs,
    sensors, frames, action interfaces and limits, plus incompatible robots.
    `providers.yaml` gives each capability a primary provider, fallbacks, a timeout and
    a minimum confidence.
  - Promotion **re-runs the evaluation** (a stored report is only a cache), lets
    simulation evidence reach `validated` but **never authorizes hardware**, and records
    lineage with the package hash and rollbacks.
  - Resolution is deterministic and never auto-selects a weak match.
  - It records the promoter as just "local", and checks only host OS and architecture
    at resolution.

## Decisions
1. **Interface contracts** (`ar.interface/1.0`). An interface is a typed API with:
   - an id (`air/service/navigation/plan_path`) and a semver version (minor additive,
     major breaking)
   - a kind (primitive, service, skill), a mode (rpc, action, stream) and an effect
     (none, compute, state, physical)
   - typed inputs and outputs, failure kinds, and whether a model may call it

   17 built-in interfaces cover navigation, localization, mapping, perception,
   manipulation, speech, memory, health, chassis and camera. Projects add
   `.air/interfaces/*.json`.
2. **Package manifests** (`ar.package/1.0`):
   - `provides`: interface plus version.
   - `requires`: interface plus version **range** (`^1.2`, `~1.2.3`, `>=1.0,<2`), optional
     or not.
   - `depends` and `conflicts`: package plus range.
   - **Hardware needs**: robot classes, catalog device types with counts, protocols,
     frames, nominal capabilities, and known-incompatible classes with a reason.
   - **Host needs**: OS, architecture, Python range.
   - The skills it carries, the config it accepts, and an entry point.

   A **deployment** (`ar.deployment/1.0`) assembles a robot from packages, with ranges,
   config and optional digest pins. Ranges use a new `core.versions.satisfies`; a
   prerelease never satisfies a range that doesn't name one.
3. **Compatibility checks** (`library.compat`):
   - **Package ↔ robot**: hardware against the manifest and the hardware catalog, host
     against this machine.
   - **Installed set**: every requirement is provided at a version in range,
     dependencies are present at acceptable versions, there are no conflicts and no
     cycles, and provided versions exist in the interface catalog.
   - **Skill ↔ robot**: required capabilities.

   These are the *package's* requirements; AIR itself still requires no hardware
   ([ADR 0011](0011-hardware-semantics-and-context.md)).
4. **Service registry** (`library.services`): Atlas-style register, heartbeat and
   lifecycle, plus rosclaw-style routing (preference, local first, priority, newest
   version). Closing their gaps:
   - Only allow-listed principals may register.
   - A provider can only claim catalog interface versions.
   - `physical` interfaces are refused: motion goes through skills and admission.
   - Inputs are validated before a call and declared outputs after it; a provider that
     breaks its contract is marked ERROR.
   - Only `none` and `compute` calls fall back to another provider. A failed `state`
     call raises `ServiceOutcomeUnknown` and is not retried.
   - Missed heartbeats make a provider STALE.
   - Every step is a trace event.
5. **Skill library** (`library.skills`, `ar.skill_release/1.0`). Releases move through
   draft → candidate → validated_sim → validated_real → deprecated → revoked (terminal),
   event-sourced in the project ledger, with packages kept in the content-addressed
   registry. Promotion:
   - **is never done by the producer**, and agents never validate;
   - needs evidence that names a **succeeded project activity**: held-out passing
     simulation or hardware evidence for `validated_sim`, and held-out passing
     **hardware** evidence for `validated_real`;
   - is blocked by any failed hardware evaluation;
   - re-checks the package against the digest recorded at release;
   - can **re-run** an evaluation (`reverify`) so an edited report authorizes nothing.

   Champions are chosen per mode by a person, and rollback is a champion change with a
   reason. Resolution is deterministic:
   - the champion if it fits, else the newest fitting version whose stage the mode allows
     (`real` needs `validated_real`)
   - rejected versions are listed with the reason
   - `registry_for(robot, skills, mode)` builds the runtime `SkillRegistry`.
6. **CLI**:
   - `air interfaces list|show`
   - `air pkg check|publish|list|deploy-check`
   - `air skills releases|promote|champion|rollback|resolve|lineage`

## Consequences
- Example packages: a chassis primitive, a lidar navigation service, and an inspection
  skill package with a deployment for the example quadruped. They resolve together; the
  navigation service is rejected on the quadcopter for three stated reasons.
- Packages declare entry points but AIR does not start processes. Launching and
  supervising package processes (robonix `rbnx boot`) is a separate step.
- The service registry is in-process. A networked registry would implement the same
  interface, and must authenticate registrations; robonix's Atlas does not.
- Interface schemas are AIR's own. Mapping them onto ROS 2 `.srv`/`.action` types or
  robonix contracts is adapter work.

## Not copied
- robonix's unauthenticated registration.
- rosclaw's anonymous promoter, its YAML and pydantic dependencies (AIR stays
  stdlib-only), and self-evolution that rewrites skills in place. In AIR a new behaviour
  is a new version, which goes through the gate.
- No code was copied from either project; only the patterns above.
