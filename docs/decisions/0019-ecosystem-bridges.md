# 0019. Ecosystem bridges, the MCP server and repository hygiene

Status: Accepted
Date: 2026-10-22

## Context

The research backlog ([oss-adoption-plan](../research/oss-adoption-plan.md) W3.6, P1/P2;
[catalog-backlog](../research/catalog-backlog.md) W3.6 amendment, Wave 6) asks for a second,
materially different integration (gate G02), more transports (DDS, CAN), bridges to ROS 2,
VDA 5050, policy servers and dora, an MCP server for AIR skills, and repository hygiene
(news fragments, a REP 2004 quality declaration, agent dispatch docs, `air doctor`).

Constraints: the core stays stdlib-only and never imports an integration; nothing may
require hardware; tests are offline and deterministic; no remote API shape is invented.
No robot, simulator, broker or ROS installation was available, so every bridge is tested
against a fake peer that speaks the wire protocol **as read from the upstream source**.

## Decisions

1. **One shape for every bridge.** Each bridge is a `RobotAdapter` (or, for policy servers,
   a client) under `integrations/<name>/`, with an injected transport (`use_transport`,
   `use_channels`, `use_can`, `use_actions`, `use_publisher`), a production binding that
   imports its SDK lazily (marked untested), a fake peer, a `skills.json`, and an explicit
   `register()`; no entry points were added (the brief allows only extras in
   `pyproject.toml`; the lead may add entry points). Example manifests under
   `examples/robots/` use hardware catalog `type_id`s and pass `air hardware check`.
   Shared helpers live in `integrations/common.py`: revision-tracked capability reports,
   observations stamped in the runtime clock at receipt (peer clocks are never mixed in),
   bounded `wait_until`, and an `InterruptGate` so `execute` returns only after an
   in-flight `interrupt` reported its own stop confirmation (otherwise the runtime would
   settle a cancel before the confirmation arrives).
2. **Microduck (W3.6, G02).** JSON-RPC 2.0 NDJSON to robotd (`hello` with
   `api_version` 44, `robot.subscribe` for `robot.state`, discrete intents as requests,
   `robot.move` as a notification refreshed every 0.1 s under the 500 ms deadman). Freshness
   uses a state sequence number, not time (host monotonic clocks can be ~15 ms coarse).
   A fall or pick-up *restricts* locomotion (report, not gate) and fails a running walk;
   a stale stream or closed socket after start is UNRESOLVED; `do_skill` has no completion
   event and so stays REPORTED_COMPLETE; the capability evidence carries `joint_limits:none`.
   Conformance runs against `FakeRobotd`, which enforces unknown-field and
   notification-vs-request rules.
3. **Unitree.** Sport-mode RPC ids become posture skills verified from
   `SportModeState_.body_height`; `Move` is fire-and-forget (no reply) and refreshed from a
   closed loop on `position`; RPC timeouts (3104, 4201) are unknown outcomes, never
   retried. `set_joint_position` writes `LowCmd_`-shaped samples only with the `low_level`
   capability (config-enabled, operator-configured joint limits, sport service observed
   released via `motion_switcher`, re-checked before writing), fresh `rt/lowstate`
   (checked before every write; stale mid-write raises), and reports requested vs applied.
   No cyclonedds import in tests.
4. **AgileX Piper.** CAN activation is an operator step: AIR reads operstate and bitrate
   (sysfs) as `can.up` and as a `manipulation` limiting factor naming `can_activate.sh`,
   and never activates anything. Freshness comes from the SDK frame `time_stamp` advancing
   (the SDK keeps returning the last frame after the bus dies). Targets are clamped to the
   limits documented in `JointCtrl`; interrupt is the SDK quick stop, confirmed only when
   the arm status reads emergency stop.
5. **ROS 2 actions (W6.2).** AIR admission first; goal rejection is a clean FAILED
   (`goal_rejected`); verification reads the action **result** (`status == succeeded`),
   never goal acceptance; CANCELED and ABORTED are failures (no Nav2 CANCELED->SUCCESS);
   a missing result is UNRESOLVED. MoveIt preflight sends the same `MoveGroup` goal with
   `planning_options.plan_only = true` and executes only on `error_code.val == 1`. Goal
   templates substitute `$args.x` strings only (no evaluation).
6. **VDA 5050 v2/v3.** Orders with a released base `[current] -> [target]`, per-topic
   `headerId`, topic levels per version (`uagv/v2/...`, `vda5050/v3/...`), the v2/v3 field
   differences (edges' node ids, `SINGLE`, `CONNECTIONBROKEN` vs `CONNECTION_BROKEN`,
   battery, position, e-stop names). Completion from `state`; order errors referencing our
   `orderId` are failures; a broken connection mid-order is UNRESOLVED (the vehicle may
   finish its released base). `cancelOrder` is confirmed when its state is FINISHED.
   Generated messages are validated in tests against the published JSON schemas (vendored
   under `tests/data/vda5050`, MIT), which caught a real bug: v3 instant actions must use
   `blockingType: NONE`.
7. **Policy clients.** openpi: metadata frame first, msgpack observation/action frames,
   text frame = server traceback; a stdlib msgpack codec implements openpi's ndarray
   convention (never pickle); every wait has a timeout, connects are bounded, a timeout
   discards the connection (a late reply would pair with the next request), and action
   chunks are refused once older than `max_chunk_age_s`. LeRobot: stdlib mirrors of
   `FeatureSpec`/`PolicyCapabilities` validation and a compatibility check before motion;
   the remote wire protocol is **not** modelled (mapping layer only). Policy clients are
   not adapters; robot-side skills that use them still go through admission.
8. **dora (W6.1), mapping layer.** AIR skills as a dora `Operator` (`command`, `cancel`,
   `tick` inputs; `status`, `result` outputs; `goal_id`/`request_id` pass through;
   `goal_status` is `succeeded` only for VERIFIED), run on a background event loop, plus
   a static dataflow check (`validate_dataflow`). Type annotations (dora's own validate)
   and record/replay are not covered.
9. **MCP server (W6.3).** A stdlib, dual-era stdio server: `initialize` for 2025-06-18 and
   2025-11-25; stateless 2026-07-28 with `_meta` protocol fields, `server/discover`,
   `resultType`, `ttlMs`/`cacheScope`, `-32022`. Tools are derived from skill specs and the
   server's delegation; a call is a proposal that goes through `Runtime.submit` and returns
   the handle state; `air.command_status` reports VERIFIED only from the runtime. Every
   skill with side effects asks an injected approver; default `deny_all`; a timeout or an
   approver error is a denial; approvals and denials are traced. Cancel needs no approval.
   Unknown tool/malformed params are JSON-RPC errors; bad arguments, denial and admission
   rejection are `isError` results. Annotations are hints only. Excluded: shell, generic
   topic publishing, connection-choosing tools, name-based dispatch. `air mcp tools|serve`
   (`--allow SKILL` is a human pre-approval at launch). The 2026-07-28 MRTR elicitation
   pattern is not used for approval yet.
10. **Hygiene.** `changelog.d/` news fragments with `towncrier.toml` (one fragment per
    group, generic, for the lead to reword) and an advisory `news.yml` workflow (warning
    only; `ci.yml` unchanged); `QUALITY_DECLARATION.md` against REP 2004 targeting QL2,
    honestly at QL4 today; a root `AGENTS.md` dispatch table and per-package `AGENTS.md`
    (genie_sim pattern); `air doctor`, whose probes are isolated and which always exits 0
    (unless `--strict` and a probe failed).

## Consequences

- G02 now has a second, materially different integration with conformance against a fake
  robotd; the conformance-against-`duck-sim` part of W3.6 still needs the real stack.
- Five new optional extras (`piper`, `vda5050`, `openpi`, `dora`, `lerobot`); the core
  still has zero runtime dependencies, and no test imports an SDK.
- No new contracts and no schema changes.
- Wire models are pinned to the upstream revisions below; upstream drift (Microduck's
  API is pre-release) will show up as protocol errors, not silent misbehaviour.

## Sources read (with licenses)

| Project | Revision | Files read | License |
|---|---|---|---|
| pollen-robotics/microduck | 942e58e | `duck-ipc-proto/src/lib.rs` (wire format, `API_VERSION`, method names, error codes, `HelloParams/Result`, `MoveParams`, `EnableParams`, `DoParams`, `IntentResult`, `HealthResult`, `SubscribeParams/Result`, `RobotState`, `MoveState`, `SafetyState`, `LoopState`, `OdomState`, `JOINT_NAMES`, `pad_link::DEADMAN_MS`), `README.md`; plus research source 06 | Apache-2.0 |
| unitreerobotics/unitree_sdk2_python | 814556d | `go2/sport/sport_api.py`, `go2/sport/sport_client.py`, `rpc/client.py`, `rpc/client_base.py`, `rpc/internal.py`, `core/channel.py`, `comm/motion_switcher/motion_switcher_{api,client}.py`, `idl/unitree_go/msg/dds_/_{LowCmd_,LowState_,MotorCmd_,MotorState_,IMUState_,SportModeState_}.py`, `utils/crc.py`, `example/go2/low_level/{go2_stand_example,unitree_legged_const}.py`, `example/go2/high_level/go2_sport_client.py`; code search for `sportmodestate` (`example/a2/sport/a2_sport_state.py`) | BSD-3-Clause |
| agilexrobotics/piper_sdk | b847e53 | `interface/piper_interface_v2.py`, `hardware_port/can_encapsulation.py`, `can_activate.sh`, `LICENSE` | MIT (its dependency python-can is LGPL-3.0) |
| ros2/rclpy | 6b5b070 | `rclpy/rclpy/action/client.py` | Apache-2.0 |
| ros2/rcl_interfaces | 614f3c4 | `action_msgs/msg/GoalStatus.msg`, `action_msgs/srv/CancelGoal.srv` | Apache-2.0 |
| moveit/moveit_msgs | 6066314 | `action/MoveGroup.action`, `msg/PlanningOptions.msg`, `msg/MoveItErrorCodes.msg` | BSD-3-Clause |
| VDA5050/VDA5050 | 0b2ae43 (3.0.0) and tag 2.1.0 | `json_schemas/{order,instantActions,state,connection,factsheet,responses}.schema`, `VDA5050_EN.md` (topic levels, cancelOrder, instant actions, connection) | repo MIT; specification text VDA-owned |
| Physical-Intelligence/openpi | 215abfb | `packages/openpi-client/src/openpi_client/{websocket_client_policy,msgpack_numpy,base_policy,action_chunk_broker}.py`, `src/openpi/serving/websocket_policy_server.py` | Apache-2.0 |
| huggingface/lerobot | 8a12558 | `src/lerobot/inference/contracts.py`, `src/lerobot/policies/chunk.py`, `src/lerobot/remote_inference/README.md` | Apache-2.0 |
| dora-rs/dora | 5a2d379 | `docs/api-python.md`, `docs/cli.md`, `examples/python-operator-dataflow/{dataflow.yml,file_saver_op.py}` | Apache-2.0 |
| modelcontextprotocol/modelcontextprotocol | 80041ab | `docs/specification/2026-07-28/{server/tools,server/discover,basic/index,basic/versioning,basic/transports/stdio,basic/transports/index,changelog}.mdx`, `docs/specification/2025-06-18/server/tools.mdx` | Apache-2.0 / MIT (transition); docs CC-BY-4.0 |
| AgibotTech/genie_sim | 6ca11c7 | `AGENTS.md` (layout pattern only) | license unclear: pattern only |
| ros-infrastructure/rep | 11ca24a | `rep-2004.rst` | public domain REP text |

Patterns were adopted; no code was copied. The VDA 5050 JSON schemas are vendored as test
data with their MIT notice. ros-mcp-server and ROSA are counter-examples (anti-pattern
charter), not sources.

## Not done / needs the owner

- **Real-stack conformance:** Microduck against `duck-sim`, Unitree against
  `unitree_mujoco`, ROS 2 against a sourced distro, VDA 5050 against a broker. The
  production bindings (`UnixSocketTransport` on a robot, `SdkChannelLayer`, `PiperSdkLayer`,
  `RclpyActionLayer`, `PahoLink`, `WebsocketsTransport`) are untested here.
- **Not verified, so configurable or omitted:** the Go2 sport-state topic (the A2/AS2
  examples use `rt/lf/sportmodestate`); the meaning of `SportModeState_.mode`; Go2 joint
  limits (operators must configure them); the body-height thresholds for posture skills
  (Go2-class assumption, skills marked experimental); `GetArmGripperMsgs().gripper_state`
  attribute name in piper_sdk; the robotd HTTP/SSE control lane; the VDA 5050 v2
  factsheet speed field; LeRobot's Zenoh wire protocol; dora type annotations and
  record/replay; MCP MRTR/elicitation-based approval.
- Entry points for the new adapters (the brief limited `pyproject.toml` to extras).
- `QUALITY_DECLARATION.md` should be linked from the README (README is lead-owned), and
  QL2 needs 1.0.0, a licence decision with DCO/CLA, branch protection, coverage and macOS CI.
- Ledger entries for this work (`air activity record --kind decision ...`) are for the lead.
- The `changelog.d/group1..7` fragments are generic placeholders for the other groups.
