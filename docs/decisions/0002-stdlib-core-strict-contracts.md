# 0002. Standard-library core and strict, versioned contracts

Status: Accepted
Date: 2026-10-22

## Context
Baseline section 5: the minimal core must not require a GPU framework, database,
simulator, ROS installation or hosted account. Section 4 asks for common semantics and
explicit schema versions.

## Decision
The core uses only the standard library. Contracts are frozen, keyword-only dataclasses
registered with `@contract(name, version)`. They serialize to `$schema`-tagged JSON and
parse strictly: unknown fields, wrong types and major-version mismatches are errors.
A minor version adds optional fields.

## Consequences
No pydantic or jsonschema dependency. Typos in manifests fail loudly. Schema evolution
needs care, as described in CLAUDE.md rule 2.
