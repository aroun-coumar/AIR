# 0011. Hardware semantics, domain packs, task context and custom components

Status: Accepted
Date: 2026-10-22

## Context
The hardware catalog ([ADR 0010](0010-hardware-catalog.md)) says what a part *is*.
The owner asked for four more kinds of context:
- **semantics:** what the data means to other systems
- **domain context:** the engineering knowledge of D01–D19
- **task context:** which facts a given job needs
- **custom component context:** vendor parts, per-unit values and documents

The owner also set a constraint: **AIR must not introduce hardware dependencies.**

## Decisions
1. **No hardware requirement, ever.** The catalog, robot classes and domain packs
   *describe*; they never *prescribe*.
   - `check_manifest` keeps two kinds of finding apart:
     - **Data errors** (always errors): unknown ids, missing or invalid values for a type
       the manifest itself chose, wrong units, changed documents.
     - **Composition advice** (warnings; errors only with `--strict`): robot-class slot
       counts, domain rules, untyped parts.
   - Nothing in the runtime, adapters or agents requires a catalog entry.
   - The package still has no dependencies, and only the CLI imports `hardware`.
2. **Custom components.**
   - A type may `extends` another. The child inherits properties, telemetry,
     maintenance, interfaces, failure modes and references, and overrides by name.
     `lineage()` gives the chain. Cycles are rejected.
   - A specialization may tighten its parent but never weaken safety. It cannot clear
     `safety_relevant`, and loosening a limit of a safety-relevant parent is a catalog
     problem.
   - Manifest components (still `ar.component_manifest/1.1`, which is not yet published)
     gain:
     - `limits`: per-unit overrides with a reason
     - `documents`: datasheets pinned by SHA-256 and checked by `air hardware check`,
       added with `air hardware attach`
     - typed `relations`: mounted_on, powered_by, drives, senses, communicates_via,
       protects, cools, controls; each has a named inverse
     - `sensitivity`
   - Every effective limit names its source: `catalog:<type>` or
     `manifest:<component> (<reason>)`.
3. **Semantics.** Vocabulary links are computed by rules, not hand-annotated on 648 types.
   - Units map to QUDT units and quantity kinds. A unit with no confident QUDT equivalent
     is exported as a literal, never a guessed IRI.
   - Categories map to W3C SOSA/SSN and ETSI SAREF classes.
   - Registry references become structured equivalents: OMA LwM2M object URNs, and CSA
     Matter device-type ids as `{scheme, id}` pairs, since Matter defines no IRIs.
   - Exports:
     - a W3C WoT **Thing Model** per device type; a Thing Model rather than a Thing
       Description, because manifests have no protocol bindings
     - a JSON-LD graph of a robot: a `sosa:Platform` hosting its components
   - `bind_observation` ties `<component>.<signal>` readings to their signal, unit
     (converted, dimension-checked) and effective limits.
4. **Domain packs** (`ar.domain_library/1.0`, 19 packs). Each has vocabulary, standards,
   invariants, a failure taxonomy, recurring checks and declarative rules.
   - Rules come in five closed kinds (component, relation, property, interface, max
     count), so a pack can never execute code.
   - `problems()` requires every pattern to match the catalog.
   - The same packs drive validation (as advice) and agent briefings (`air hardware brief`).
5. **Task context** (`hardware.context.build_context`):
   1. Seed components from the focus. Words naming *what a part is* outrank words that
      only match a signal name.
   2. Walk the component graph.
   3. Gather facts: what each part is, limits with sources, readings, failure modes,
      maintenance, documents, ledger history, open triage items, findings and domain
      invariants.
   4. Rank, and cut to a character budget.

   Every fact has a source and a sensitivity. Restricted facts are withheld unless every
   model stays on the device. The work-agent tool `hardware_context` (on by default for
   diagnosis and maintenance) decides this from the task's data classification and
   egress setting. A fingerprint over manifest, catalog, focus and ledger head lets
   static facts be reused (REQ08). `ContextBuilder(catalog=...)` adds a compact hardware
   section (safety parts, out-of-limit readings) to the planning agent's view.

## Consequences
- `air hardware context | attach | export | brief`. `check` applies domain rules and
  verifies documents.
- Domain-pack content was written by a researcher and checked mechanically against the
  catalog. Standards in packs, like those in the catalog, are pointers to verify.
- Ranking is heuristic: word overlap, graph distance, safety and reading status. It is
  deterministic and explainable, not learned. It can be tuned with evidence later.
- The `air:` vocabulary namespace is AIR's own and has no published vocabulary document
  yet.
