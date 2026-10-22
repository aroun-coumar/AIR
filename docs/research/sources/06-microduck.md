# Research source 06: Pollen Robotics Microduck

Raw findings from the 2026-10-22 deep read (researcher report, condensed). Snapshots:
microduck HEAD 2026-10-21; microduck_rl `develop` b02fdea; arena 0dd3967; challenges
6ff4070; yocto 9169123. Not reachable: `microduck_runtime`, `microduck_brain`,
`microduck-simulator` (repository not found). No code was run. The synthesis is in
[../oss-adoption-plan.md](../oss-adoption-plan.md).

## Architecture (`pollen-robotics/microduck`, Rust)

- **Seven systemd daemons** on an RK3566:
  - `robotd`: control loop, servos, policies, safety, health
  - `configd`: wifi, identity, pairing
  - `updaterd`: signed releases, health gate, rollback
  - `btd`: BLE
  - `padd` / `netpadd`: gamepads
  - `mediad`: camera, WebRTC, perception
  - `tofd`: time-of-flight sensor
- **Invariants:** the recovery path (configd, updaterd, btd) survives a dead robotd;
  robotd is authoritative on safety; the loop never blocks on another service; each
  piece of state has a single writer.
- **IPC:** a unix socket per service carrying JSON-RPC 2.0 as NDJSON; `SO_PEERCRED` and
  uid/gid allowlists gate mutating calls. The same messages run over BLE, a WebRTC
  datachannel and an HTTP/SSE "control lane" for server-side agents. gRPC, D-Bus,
  HTTP/WS and zenoh were explicitly rejected on dependency cost.
  `duck-ipc-proto/src/lib.rs` is at `API_VERSION = 44` with about 50 methods.
  Continuous intents are notifications (last writer wins, timestamped); discrete
  actions are requests.
- **Control loop:** one tokio task at 50 Hz with `MissedTickBehavior::Skip`. Each tick
  reads the bus, builds the observation, runs ONNX, clamps and writes. There is
  **deliberately no RT tuning** (no SCHED_FIFO or mlockall). Health (achieved Hz,
  missed deadlines) comes from atomics. 50 Hz was "never re-derived". **The design
  calls for a watchdog, but the systemd unit has only `Restart=always`, and no
  sd_notify was found.**
- **Policies:** ONNX on the CPU through `ort` (`INTRA_THREADS=1`). The NPU is used only
  for perception. Contract: `obs[1,61] → actions[1,14]`. Shape and type are validated
  at load, a warm-up inference runs, and non-finite outputs are rejected. A hot swap
  happens on a worker thread, and **a failed load keeps the running controller**.
- **Safety** (`duck-control/src/safety.rs`): owns the only write handle (enforced by the
  borrow checker); non-finite targets are refused; clamps cover the actuator range only
  (**no per-joint anatomical limits**); 500 ms intent deadman, where stop means stand.
  The fall verdict *reports but does not gate*; a separate `limp_fall` recovery runs on
  its own. Pickup classifier; battery shutdown at 6.6 V. **No software thermal limit**
  (hardware shutdown only).
- **Bring-up:** never moves just because it started; adopts the current pose, then
  Limp → enable → Homing → Ready. `safeToRestart` is false while moving. Only
  release-blamable causes enter the health verdict; battery and temperature are
  description only.
- **Updates:**
  - App level (`updaterd`): versioned release directories, an atomic symlink,
    minisign signatures against a set of trusted keys, then manifest signature, then
    sha256, then artifact signature. A health gate with rollback; a two-boot crash
    counter; `degraded` commits rather than reverting; a `min_supported` floor; an
    fsynced update log; pluggable sources (GitHub, HF Hub, HTTP, local).
  - OS level (`microduck_yocto`): RAUC A/B slots marked good after 180 s, with U-Boot
    fallback.
- **SDK:** none shipped yet; a Python client is roadmap work. HF Spaces reuse a JSON-RPC
  `Rpc` class. **The simulator (`duck-sim`) runs the real daemons against a MuJoCo
  body.**

## Sim-to-real (`microduck_rl`)

- mjlab 1.3.0 (MuJoCo Warp) with rsl_rl PPO; 13 task families; 50 Hz
  (0.005 s × decimation 4); the BAM XL330 actuator model is "most of the sim2real gap".
- **Domain randomization:** CoM (curriculum), mass and inertia, friction, armature,
  pushes, IMU mounting, encoder bias, battery voltage and sag, command delay. kp/kd
  randomization is off.
- `AGENTS.md` "Invariants" and "Sim2real footguns" are a good distilled playbook.
- **Seed and DR configuration are not in the policy manifest.** DR is set by Python
  globals, so it can only be recovered from the commit and dirty flag.
- **Publish:** `uv run publish` checks the shape, smoke-runs 50 steps (rejecting NaN
  or constant output), writes a schema-2 manifest, and uploads to a private HF repo
  by default. On-robot gates: `model_api`, `obs_len`, `action_len`, `robot.model`.
- **Origin classes:**
  - official (`pollen-robotics/*`): signed, semver, auto-update
  - community: repo, revision and sha recorded, **unsigned**, never auto-applied
  - local: no provenance

  A bad community override at boot falls back to official and reports `degraded`.
- **Arena:**
  - Server-side MuJoCo evaluation at a **pinned Hub revision**, with sha256 of the
    weights and a size cap.
  - Each event's TOML sets the course, perturbation, termination,
    `seeds=[1..5]` and a speed sweep.
  - Scoring: finalists, then a multi-seed mean with DNF counted at the limit, and a
    median replay.
  - `RUNNER_VERSION` is compared only within a version; MuJoCo is pinned.
  - A per-platform **golden run** is re-measured in tests and the Docker build. They
    found arm64 vs x86 differ by **8.4%**, and fixed cross-CPU bit-exactness with
    libm math.
  - `arena recover` **re-measures** from tagged policies instead of trusting stored
    scores.
  - Gaps: public fixed seeds, no hidden set, sim-only ranking.

## Licenses

| Repo | License |
|---|---|
| microduck, microduck_yocto, microduck_emotions, microduck-gst-plugins | Apache-2.0 (gst binaries are built from upstream with other licenses [U]) |
| microduck_rl | Apache-2.0 code; **STL meshes CC BY-NC-SA 4.0** (`LICENSE-MODELS.md`) |
| **microduck-arena, microduck-challenges** | **no LICENSE**, so all rights reserved |
| mjlab, Open_Duck_Mini, reachy_mini | Apache-2.0 |
| HF `microduck-policies` | apache-2.0 card |

Talking JSON-RPC to robotd is fine. Do not vendor the NC meshes. Do not copy
arena or challenges code; reimplement the ideas.

## Adoption items

| # | Pattern | AIR module | Req / gate | P | Effort |
|---|---|---|---|---|---|
| A1 | **Microduck RobotAdapter** over JSON-RPC/NDJSON (unix socket, HTTP lane, or duck-sim) | `integrations/microduck/` | G02, REQ03, REQ05 | P0 | M |
| A2 | Intent deadman: continuous skills have a refresh period and keepalive under `max_input_age_s` | `contracts/execution.py` | REQ12, G05 | P0 | S |
| A3 | "Report vs gate": a fall publishes a RESTRICTED capability; robot-owned recovery is not commanded by AIR | `capability.py`, `admission.py` | REQ14 | P1 | S |
| A4 | Health verdict only from release-blamable causes; other factors are description | future `health/` | REQ14, REQ16 | P1 | S |
| A5 | **Policy as a content-addressed entity with load gates; a failed load keeps the old policy** | `registry.py`, provenance, `run_policy` admission | REQ02, REQ16, G03 | P0 | M |
| A6 | Origin classes official/community/local with different trust; non-official needs explicit authority | `registry.py`, authority | REQ16 | P1 | S |
| A7 | **Physics fingerprint**: golden run per platform, simulator pin, runner version; compare within platform plus version | future `engineering/`, ExperimentRecord | REQ02, G03 | P0 | S |
| A8 | Re-measure instead of trusting stored scores | provenance, `air evaluate --reverify` | G03 | P2 | M |
| A9 | Scoring: sweep, finalists, multi-seed mean, DNF at the limit, median replay | `engineering/` | REQ02, REQ04 | P1 | S |
| A10 | Record update events (signed key set, health-gated rollback, boot counter, min_supported) as deployment activities | provenance | REQ16 | P1 | L |
| A11 | Single writer to actuators; document that the adapter is the only actuator path | `plugins.py` docs | REQ05 | P2 | S |
| A12 | State reports `requested`/`applied`/`limited_by` | ExecutionResult | REQ13, REQ14 | P1 | S |
| A13 | Import the vetbook lifetime counters (hours, metres, falls, servo swaps) into MaintenanceRecord exposure | adapter, provenance | REQ16 | P2 | S |
| A14 | Per-experiment AGENTS.md plus TOML with bounded tunables | future `engineering/` | REQ04, REQ18 | P2 | M |
| A15 | **Conformance against the real stack driving a simulated body** (duck-sim) in CI | `conformance.py` | REQ18, G02 | P1 | M |
| A16 | Avoid import-time side effects in plugin loading (`train_hook.py` calls `sys.exit` during import) | `plugins.py` | - | - | - |

### As G02's second, materially different integration

**Yes, with caveats.**
- It is real and cheap, with an open stack.
- It differs from the fakes: a learned closed-loop policy; continuous intents with no
  completion event; an external safety authority; a simulator twin that runs the real
  daemons.
- Caveats:
  - the protocol is pre-release (API 44): pin and handshake;
  - no SDK yet;
  - odometry drift on real hardware is unmeasured;
  - AIR stays a high-level controller (the 50 Hz loop is robotd's).

Sketch:
- **Manifest:** a dynamixel bus, 15 servos, IMU, ToF, camera, and the policy as a
  component with a sha256.
- **Profiles:** locomotion (RESTRICTED when fallen, held, degraded or low on battery),
  head, skills, `control_loop` (achieved Hz, missed deadlines).
- **Skills:**
  - `walk_to` (experimental, wide tolerance)
  - `turn`, `stand`/`sit`
  - `play_emotion` (upstream API [U])
  - `run_policy(slot, entity_id)`: the postcondition checks that the loaded sha
    matches the registry entity.
- **`walk_to` contract:** periodic refresh every 0.1 s, `max_input_age_s=0.25`
  (below the 0.5 s deadman), FIRM, REPLACE_LATEST, fallback stop (= stand).
- **On a fall:** FAILED with reason `fallen` (UNRESOLVED if the link is lost), and the
  profile is downgraded until the robot is observed upright again.

**Provenance chain for a trained policy:**
1. training activity (versions, commit, **seed**, full DR and reward config entity,
   environment)
2. policy entity (sha256, dims, model_api, Hub `repo@commit`, derived_from)
3. sim evaluation ExperimentRecord (runner_version, platform, golden check, seeds,
   per-seed results)
4. deployment activity (robot, slot, origin, daemon release, previous policy)
5. on-robot evaluation (AIR command spans, health, loop Hz and missed deadlines,
   falls and pickups)

## Anti-patterns and gaps

- a watchdog that is designed but not implemented
- no per-joint or thermal software limits (AIR must not imply protection that doesn't
  exist; mark `joint_limits: none`)
- unsigned community policies running on hardware
- seed and DR config missing from the manifest
- fixed public evaluation seeds
- import-time side effects in plugins
- coupling to a rapidly changing wire protocol
- NC-licensed meshes inside an "Apache" repo; unlicensed repos
- comparing sim scores across platforms
