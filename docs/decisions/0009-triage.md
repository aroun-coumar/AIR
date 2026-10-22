# 0009. Triage: one queue for everything AIR finds wrong

Status: Accepted
Date: 2026-10-22

## Context
AIR produced many signals but had no triage, so each one stopped where it was found:
- Runtime outcomes: `unresolved`, `failed`, cancels without a confirmed stop, adapter and
  interrupt errors, admission rejections.
- Leases that expired on occupied resources.
- Work-agent results: pending proposals, denied approvals, sandbox and model refusals.
- Checks: ledger integrity, the requirements matrix, the source catalog.
- Invalidated calibration.

Nothing collected these, ranked them, gave them an owner or a deadline, or tracked
them to a decision. The owner asked for triage across the whole workflow, built on
mechanisms from open-source tools.

## Decisions
1. **Signals become items** (`agentic_robotics.triage`, `ar.triage_item/1.0`).
   - Collectors emit `Signal`s.
   - The first matching routing rule sets severity, priority, owner, escalation chain,
     deadlines and the fields that make up the fingerprint (`group_by`).
   - A repeated fingerprint becomes an occurrence on the existing item (count, last
     seen, evidence), not a new item.
   - A signal whose evidence is already recorded is ignored, so sweeps are idempotent.
2. **Severity and priority are separate axes.**
   - Severity is impact: `safety`, `integrity`, `mission`, `quality`, `hygiene`.
   - Priority is urgency: `P1`–`P4`.
   - Safety and integrity are *protected* severities.
3. **States:** `new → triaged → assigned → in_progress → resolved | dismissed`, plus `snoozed`.
   - `escalated` is entered when a deadline passes.
   - `regressed` is entered when a resolved fingerprint comes back.
   - `reopen` takes a dismissed item back to `new`.
   - Moves outside the transition table are refused.
4. **Deadlines and escalation.**
   - An unacknowledged item escalates along its rule's chain when its acknowledgement
     deadline passes, then again each time the renewed deadline passes.
   - It escalates once more if the resolve deadline passes.
   - Acknowledging an item stops acknowledgement escalation.
5. **Suppression never hides what matters.**
   - Silences (matchers, start and end, creator, comment) and inhibitions (an open
     cause hides its symptoms when chosen labels are equal) apply only to unprotected
     items.
   - Agents cannot create silences.
   - An item that matches both sides of an inhibition rule cannot inhibit itself.
6. **Decisions belong to people.**
   - Every decision names a principal. The triage system itself (`system:triage`) only
     escalates, regresses and auto-resolves.
   - Agents (`agent:*`) may acknowledge and work items, but cannot resolve or dismiss
     protected items.
   - Dismissing needs a reason. Resolving a safety item needs an evidence reference.
7. **Cleared conditions resolve themselves.** Sweep-checked conditions with
   `auto_resolve` (pending proposals, requirement evidence, source catalog) resolve
   when they clear. Protected items never auto-resolve.
8. **Event-sourced on the ledger.**
   - State changes are `triage.opened`, `triage.occurred`, `triage.transition`,
     `triage.suppressed` and `triage.silenced`/`unsilenced` entries.
   - The book replays them, so triage history is hash-chained, verifiable and readable
     by any process.
9. **Policy is data.**
   - The built-in table (`default_policy()`) can be written to `.air/triage.json` and
     edited (`air triage rules --write`).
   - The policy is itself a contract (`ar.triage_policy/1.0`).
10. **Where signals come from.**
    - `RuntimeTriageSink` (a trace sink) and `resource_listener` (a resource-manager
      listener) collect live.
    - `air triage sweep` collects everything else, then auto-resolves cleared conditions
      and escalates overdue items.
    - `air context` shows the open items.

## Refinements after review
- **Fingerprints** are full SHA-256 over the rule's `group_by` labels and a grouping
  version.
  - Items record `grouping_version` and the group labels a signal lacked
    (`missing_group_labels`).
  - Items made by an older grouping version are re-keyed, not duplicated.
- **Content hash.** An exact repeat (same evidence, same title and labels) is dropped. A
  changed repeat updates the item's title and labels, like Keep's full vs partial
  duplicates.
- **Severity only rises automatically** when later signals fold in. Priority takes the
  most urgent value, and escalation raises it by one level.
- **A person's priority is locked** (`air triage prioritize`): rules and escalation no
  longer change it.
- **Protected items:**
  - A dismissed protected item that recurs is reopened, not just counted.
  - Protected items must be acknowledged before they can be resolved (no direct
    `new → resolved`).
- **Silences:**
  - A silence needs at least one matcher that does not match an empty value.
  - It is capped by `max_silence_s` (7 days by default), like Alertmanager's rule that a
    silence cannot match everything.
- **Snoozed** is a state with a wake condition: a time (up to `max_snooze_s`) or a
  number of further occurrences, as with Sentry's "archive until". It is never allowed
  for protected items.
- **Stale.** A sweep check that cannot run marks its open items `stale`; they are never
  resolved. The mark clears when the check runs again.
- **Escalation chains** are copied onto the item when it opens, so a later policy edit
  does not reroute open work.

## Sources
Mechanisms were re-implemented, not depended on. All were verified on 2026-10-22
against the default branches.

| Mechanism | Source (license) | Read |
|---|---|---|
| First-match routing with `group_by`; label matchers `=`, `!=`, `=~`, `!~` (regexes anchored; a missing label equals an empty value) | prometheus/alertmanager (Apache-2.0) | `dispatch/route.go`, `config/config.go`, configuration docs |
| Inhibition with `source_matchers`, `target_matchers`, `equal`; two-sided matches excluded so nothing inhibits itself | prometheus/alertmanager | `inhibit/inhibit.go` |
| Silences with matchers, `starts_at`/`ends_at`, `created_by`, required `comment`; ending a silence sets its end to now | prometheus/alertmanager | `silence/silence.go`, `silence/silencepb/silence.proto`, `api/v2/openapi.yaml` |
| Fingerprint from a chosen, sorted set of label fields | prometheus/common `model/labelset.go`, `signature.go` (FNV-1a over sorted labels); AIR uses SHA-256 over canonical JSON, as keephq/keep does | prometheus/common (Apache-2.0); keep `keep/api/alert_deduplicator/alert_deduplicator.py` (MIT outside `ee/`) |
| Configurable fingerprint fields per rule | keephq/keep `AlertDeduplicationRule.fingerprint_fields`, `ignore_fields` | `keep/api/models/db/alert.py` |
| Cleared conditions resolve; pending vs firing | prometheus/prometheus (Apache-2.0) | `rules/alerting.go` (inactive, pending, firing; resolved alerts re-sent) |
| Regression when a resolved issue reappears; ignore until a condition; custom fingerprints | getsentry/sentry (FSL-1.1-Apache-2.0, fair-source; pattern only) | `src/sentry/models/group.py`, `src/sentry/types/group.py`, grouping and issue-states docs |
| Escalation chains, wait steps, acknowledgement as the stop signal; states firing, acknowledged, resolved, silenced | grafana/oncall (AGPL-3.0; archived 2026-03-24) | `engine/apps/alerts/models/escalation_policy.py`, `alert_group.py`, `constants.py` |
| `needs-triage` → `triage/accepted`; priority labels from critical-urgent to backlog; stale/rotten lifecycle | kubernetes/community, kubernetes/test-infra (Apache-2.0) | `contributors/guide/issue-triage.md`, `sig-contribex-k8s-triage-robot.yaml` |
| Severity (S1–S4, impact) separate from priority (P1–P5, urgency) | Mozilla Firefox bug-management docs | firefox-source-docs bug-mgmt, BMO field definitions |
| SEV levels; when unsure, choose the higher severity | PagerDuty/incident-response-docs (Apache-2.0) | severity-levels page |

## What AIR adds or deliberately differs on
- **Protected severities.** Alertmanager and OnCall let any alert be silenced. AIR
  never silences or inhibits safety and integrity items, and agents cannot close them.
- **Evidence required to resolve a safety item.** Typical trackers accept a free-text
  resolution. A safety resolution in AIR must point at evidence (a ledger entry, a
  command or a trace).
- **No notification transport.** AIR records owners and deadlines. Paging, chat and
  e-mail remain integrations, so the core stays dependency-free.
- **No grouping timers** (`group_wait`, `group_interval`). Items are durable records,
  not notifications, so deduplication happens at ingest.
- **Escalation runs on sweeps.** There is no background scheduler; a deployment runs
  `air triage sweep` periodically (cron, a CI job or a supervisor).

## Consequences
- The quickstart and scenarios don't attach the triage sink by default. Products opt in
  with `runtime.trace.add_sink(RuntimeTriageSink(book))`.
- Planned work builds on this queue rather than starting separate systems: the incident
  pipeline (W3.8) and health aggregation (W3.7) feed triage items.
- Owners are role names (`role:operator`), not people. Mapping roles to people and
  rotas belongs to the deployment, as on-call schedules do in OnCall.
