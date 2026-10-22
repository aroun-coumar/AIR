# Security policy

AIR is software that can command physical robots. We treat vulnerabilities that could
cause unauthorized or unsafe actuation as the highest severity.

## Reporting a vulnerability

**Do not open a public issue.** Report privately through GitHub's
[private vulnerability reporting](https://github.com/aroun-coumar/AIR/security/advisories/new)
for this repository.

Please include the affected version or commit, a description, steps to reproduce, and
the impact you expect (for example "admission bypass", "authority escalation", "ledger
tampering undetected", "code execution on asset load").

## What to expect

| Step | Target |
|---|---|
| Acknowledgement of your report | within 14 days |
| Initial assessment and severity | within 30 days |
| Fix or mitigation for high or critical issues | as fast as practical; you will get status updates at least every 30 days |
| Public advisory | after a fix is released, crediting you unless you prefer otherwise |

Release notes list the advisories and CVEs that each release fixes.

## Scope

In scope:
- the `agentic-robotics` package and `air` CLI
- the contract schemas
- the conformance suite and the reference adapters in this repository

Of particular interest:
- bypassing admission checks, authority, fencing or freshness
- forging VERIFIED outcomes
- tampering that the ledger or registry fails to detect
- executing code from assets
- leaking credentials into traces or the ledger

Out of scope:
- vulnerabilities in third-party robots, simulators or model providers (report those
  upstream)
- issues that need an already-compromised host
- missing safety functions in a robot. AIR is **not** a safety controller and never
  replaces an emergency stop, a safety PLC or a robot's own protective functions.

## Supported versions

AIR is pre-1.0. Security fixes go to the latest release only.
