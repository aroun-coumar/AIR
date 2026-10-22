# 0016. Policies, releases and engineering-domain contracts

Status: Accepted
Date: 2026-10-22

## Context
The adoption plan (W3.5 and the P1 asset items) and the catalog backlog (W3.5, W5.1,
W5.3, W5.5, W5.6) ask for six things:
- learned policies treated as content-addressed entities with load gates;
- releases that activate by trial and confirm, with signed update metadata;
- typed parameter metadata;
- dataset episodes that convert to the common formats without loss;
- one cross-embodiment action contract;
- asset cards and a versioned conformance report.

The core must stay stdlib-only, nothing may require hardware, and a report must never
stand in for an observation. New code: `contracts/{policy,release,parameters,dataset}.py`,
the `releases/` package (`policy_slot`, `activation`, `tuf`, `signing`, `records`, `cli`),
registry blobs and policies, and `ConformanceReport.to_record()`.

## Decisions
1. **Policies (W3.5): `ar.policy_manifest/1.0` plus opaque model bytes.**
   - The model file is bytes named by its SHA-256. The JSON sidecar advertises before
     admission:
     - model API, format and inference backend;
     - robot model, joint order, kp/kd gains and default joint positions;
     - observation features and dims, observation scale/offset/clip/history;
     - action feature and dim, action scale and bounds;
     - prediction steps vs execution steps and the action interval;
     - origin, exit mode, and a **mandatory** `max_action_age_s`.
   - `identity()` hashes every behaviour-relevant field together with the model digest.
     The same weights under another backend, gain or scale are a different policy.
   - The **load gate** (`PolicyRequirements.mismatches`, `PolicySlot.gate`) refuses a
     digest mismatch or any metadata mismatch, and lists every reason. **A failed load,
     including a runner factory that raises, keeps the old policy and its runner.**
   - Origin classes: `official` loads without extra authority. `community` and `local`
     need a delegation for operation `load_policy` on skill `policy:<slot>`, checked by
     the runtime's `AuthorityService`. A manifest cannot vouch for itself.
   - Action chunks (`ar.action_chunk/1.0`) carry provenance: `request_id`, slot load
     `generation`, `session_id`, `artifact` (the identity) and the observation's capture
     time. `accept()` refuses a chunk that is stale (age is measured from the observation
     when known), from another generation or session, or that widens the staleness bound.
   - GR00T template: `check_observation` runs before the opaque runner and `check_action`
     after it (shape, finite, declared bounds). Each failure is a trace event.
   - booster_deploy: starting takes two steps, `arm(principal)` then
     `start(token, principal)`, and arming expires. `stop()` returns the declared
     `exit_mode` (`hold` or `damp`).
   - The registry stores the model in a content-addressed blob store
     (`_blobs/sha256/<hex>`, logged in the publish log). `fetch_policy` re-hashes it.
2. **Releases (W5.3).**
   - **Trial and confirm (mcuboot pattern).** `ar.release/1.0` names its targets, a
     security counter and the `confirm_checks` it must pass. A release with no checks is
     refused, because confirming unconditionally is not a trial.
     - `ActivationManager`: stage, then boot (TRIAL), then confirm with **observed** check
       results.
     - A reboot while in trial, or a passed `confirm_within_s` deadline, reverts
       automatically.
     - Staging below the stored counter is refused as a downgrade. The counter rises only
       on confirm, so an unconfirmed release cannot lock out the previous one.
     - State persists as `ar.release_activation/1.0`. Each transition is a trace event
       and, with a project, a `release.*` ledger entry.
   - **TUF metadata (`releases.tuf`).** Shapes were checked against the spec on
     2026-10-22:
     - signed envelope; root keys/roles/thresholds; targets length/hashes/custom;
       snapshot and timestamp `meta`;
     - `YYYY-MM-DDTHH:MM:SSZ` expiry; keyid = SHA-256 of the canonical key.
   - `TrustedSet` follows the client order with a fixed start time:
     - root rotation: exactly N+1, signed by a threshold of both the old and the new
       root;
     - timestamp: version must rise; an equal version means "no update";
     - snapshot: must match the timestamp; no targets version decreases or disappears;
     - targets: must match the snapshot;
     - every step checks expiry against the start time (freeze attacks).
   - `registry_targets` turns registry assets into TUF targets.
   - **Director (Uptane-like).** The director is a second repository with its own root.
     Its targets entries are `<robot_id>/<slot>`, with `custom.robot_id`, `slot` and
     `image_path`. `assigned_targets` requires the director and the image repository to
     agree on length and hashes.
   - **Signing.** `hmac-sha256` uses stdlib `hmac`. It is for tests and single-owner
     setups only: it is not a TUF key type and gives no non-repudiation. `ed25519` sits
     behind the new `[signing]` extra, and `cryptography` is imported lazily through
     importlib. Canonical JSON refuses floats. No home-made cryptography.
3. **Parameters (W5.1).** `ar.parameter_schema/1.0` holds `ParameterDef` entries:
   - name, display name, description;
   - unit, which must be in `core.units.known_units()`;
   - min, max, increment; enum `values`; `bitmask` bits;
   - default, `reboot_required`, `calibration`, `read_only`, `user_level`.

   `ar.parameter_set/1.0` holds the values. `validate()` lists every problem, and
   `diff()` flags reboot and calibration changes. `record_parameter_snapshot` stores a
   set as a `parameter_set` project entity. This is a clean-room take on ArduPilot's
   metadata idea; no ArduPilot code or text was used (GPL-3.0).
4. **Datasets (W5.5).** `ar.episode/1.0` steps carry the RLDS flags `is_first`, `is_last`
   and `is_terminal`, validated (`is_terminal` only on the last step). `truncated` means
   the episode ended on a non-terminal last step. Episode metadata holds `episode_id`,
   `agent_id`, `environment_config`, task, robot type and fps.

   | AIR | RLDS-like (`to_rlds`) | LeRobot-like (`to_lerobot`) |
   |---|---|---|
   | step observation `{k: v}` | `observation` | `observation.<k>` |
   | action | `action` | `action` |
   | reward | `reward` | `next.reward` (+ `air.has_reward`) |
   | discount | `discount` | `air.discount` |
   | is_first / is_last / is_terminal | same names | frame 0 / `next.done` / `next.done and not next.truncated` |
   | timestamp | `air_timestamp` | `timestamp` (else `frame_index / fps`) |
   | index columns | - | `frame_index`, `episode_index`, `index`, `task_index` |
   | metadata | `episode_metadata` (+ `air_*`) | `episode["air.episode_metadata"]`, `info` |

   - RLDS names were checked against google-research/rlds `README.md`.
   - LeRobot names were checked against `src/lerobot/utils/constants.py`
     (`DEFAULT_FEATURES`, `next.reward`, `next.done`, `next.truncated`) and the
     `DatasetInfo` fields.
   - Round trips are tested for both terminated and truncated episodes.
   - `ar.training_run/1.0` records git state (commit, branch, dirty, diff digest) and a
     `NanCheck` (enabled, what was checked, first non-finite step). `reproducible` needs
     a clean commit and a seed. `record_training_run` records a `learning` activity that
     uses the episodes and generates the run, and it fails the activity if NaNs were
     found or the check was off.
5. **Cross-embodiment actions (W5.6).** `ar.action_space/1.0` is an ordered registry of
   named, unit-bearing slots. `pack()` places named values into one vector with a mask.
   Absent slots are masked rather than silently zero-filled, and units must match.
   `unpack()` refuses partially masked slots. `default_action_space()` is AIR's own
   small layout, not RDT's index table.
6. **Asset cards and conformance reports.**
   - `ar.asset_card/1.0` covers models, datasets, policies and skills: SPDX license,
     intended (required) and out-of-scope use, limitations, hazards, operating envelope,
     evaluation evidence links, conformance digest, requirements, maintainers,
     derived_from and superseded_by.
   - `problems()` lists a card's gaps. Cards are claims and never grant authority.
   - `ConformanceReport.to_record()` emits `ar.conformance_report/1.0` with the suite,
     AIR and contract versions and the manifest/skills digests. It is written by
     `air conformance … --out`. `passed` must agree with the outcomes, so a report
     cannot assert its own badge.

## Consequences
- Twelve new contracts at 1.0, with exported schemas: `policy_manifest`, `action_chunk`,
  `action_space`, `unified_action`, `parameter_schema`, `parameter_set`, `episode`,
  `training_run`, `release`, `release_activation`, `asset_card`, `conformance_report`.
  No existing schema changed.
- New CLI groups: `air policy`, `air release`, `air params` and `air dataset`, plus
  `air conformance --out`.
- The core is unchanged and stdlib-only. The `[signing]` extra is optional and is not
  installed in the dev venv, so the Ed25519 test skips.
- AIR never runs the model. A `PolicySlot` needs an injected runner factory, so the
  adapter or integration owns inference and the real-time loop.

## Sources and licenses
- LeRobot `src/lerobot/inference/contracts.py`, `utils/constants.py`, `datasets/utils.py`
  (Apache-2.0): field names, pattern only.
- NVIDIA Isaac-GR00T `gr00t/policy/policy.py` `BasePolicy` (Apache-2.0): check-before
  and check-after template.
- Microduck `policy-manifest.md` and microduck_rl publish gates (Apache-2.0); mjlab
  exporter (Apache-2.0); booster_deploy (two-step start, damping exit); openpi as the
  counter-example with no staleness bound.
- mcuboot design docs (Apache-2.0): test/confirm swap, revert, security counter.
- The Update Framework specification (CC BY 4.0) and python-tuf (Apache-2.0/MIT, not
  used as code); Uptane standard (director/image repositories).
- ArduPilot parameter documentation (GPL-3.0): idea only, clean-room.
- RLDS (Apache-2.0); rsl_rl (BSD-3-Clause): git state and NaN checks; RDT
  (MIT): named-slot unified action space, pattern only.
- Hugging Face model and dataset cards (Apache-2.0 docs): card fields.

## Not done / needs the owner
- TUF **delegated targets roles**, consistent-snapshot file naming, mirrors, and a
  repository-side tool that writes and rotates signed metadata files. `air release
  targets` prints unsigned targets metadata only.
- Key custody: where release keys live, threshold policy per role, rotation procedure.
  HMAC keys must not be used for real fleets.
- The Uptane **time server**, ECU version manifests and the full primary/secondary
  split. Only the director/image agreement check exists.
- Real boot integration (bootloader, A/B slots, RAUC/mcuboot hooks). `ActivationManager`
  models the state machine; an integration must call `boot()` at startup and feed
  `confirm()` with observed health.
- A `run_policy` skill and admission rule wiring `PolicySlot` into the runtime (owned
  with group 1's execution/skill contracts), and a Microduck runner.
- Conformance badge issuance, and an `air` command that publishes cards to the registry
  (cards publish through `LocalRegistry.publish` today).
- Parquet/TFRecord writers. The conversion helpers produce dicts; writing files needs
  optional extras.
- W6.6 (AGENTS.md layout, `air doctor`/`air status`) is not part of this group's changes.
- Whether the policy/action contracts should be licensed more permissively than AGPL,
  together with the other schemas (legal question in the adoption plan).
