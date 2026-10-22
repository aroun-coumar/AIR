# 0004. AGPL-3.0 license

Status: Accepted (open for review)
Date: 2026-10-22

## Context
The GitHub repository was created by the owner with an AGPL-3.0 LICENSE. Package
metadata initially said Apache-2.0. Baseline section 21 lists licensing as an open
ecosystem decision.

## Decision
Align package metadata with the repository: AGPL-3.0-or-later.

## Consequences
Products that distribute AIR, or run it as a network service, must release their
corresponding source. This may limit adoption by commercial product teams. Revisit
before a first public release if the goal is broad embedding (Apache-2.0 or MIT, or
dual licensing).
