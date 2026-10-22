# Research source 01: agentic robotics frameworks

Raw findings from the 2026-10-22 survey (researcher report, condensed). **[U]** means
unverified. The synthesis is in [../oss-adoption-plan.md](../oss-adoption-plan.md).

Licensing note: Apache-2.0, MIT and BSD code can be combined into AGPL-3.0 (keep the
NOTICE and attribution files). Ideas need no license. Repositories without a LICENSE
file are off-limits for code.

## Finding about AIR (confirmed)

`Runtime._handles` is an in-memory dict. Command deduplication (`duplicate_ignored`,
`command_id_reused`) and UNRESOLVED state are lost when the process restarts.

## Projects

| Project | License / activity | Relevant mechanism (files read) |
|---|---|---|
| NASA JPL ROSA | Apache-2.0; slowing (last push 2026-03) | `rosa.py`: LangChain tool-calling agent; `astream()` with typed events token/tool_start/tool_end/final/error; `prompts.py` sectioned persona prompts. **`tools/ros2.py` runs `subprocess(..., shell=True)` on model-built `ros2 service call` strings; the blacklist only filters listings.** |
| RobotecAI RAI | Apache-2.0; active | `BaseConnector[T]`: pub/sub, service and action triad; `HRIMessage` with communication_id/seq_no/seq_end; **`BaseROS2Tool` readable/writable/forbidden, which default to None and so allow everything**; `navigation/bounds.py` workspace box that rejects NaN; bounded sensor `BaseAggregator`; Megamind `analyzer_node` asks an **LLM to judge step success**; `@timeout` decorator |
| DimensionalOS dimos | Apache-2.0 (LICENSE notice); pre-release, very active | `@skill(lifecycle=instant|background)`; **`CapabilityRegistry` atomic, token-scoped holds ("a stale invocation can't free a live hold")**; skills exposed as an MCP server, which acquires capabilities before dispatch and returns a structured conflict; `SkillResult` "the agent decides" |
| Hugging Face LeRobot | Apache-2.0; very active | `Robot` ABC with observation/action feature schemas; **`send_action` returns the action actually applied (clipped)**; `ensure_safe_goal_position(max_relative_target)`; typed configs with a choice registry; plugin discovery by package-name prefix (import side effects); `policies/chunk.py` `FeatureSpec` (mandatory semantics), `ChunkPolicySpec(prediction_steps, execution_steps)`; `drop_queued_actions`; dataset `CODEBASE_VERSION` compatibility check |
| openpi | Apache-2.0 | `BasePolicy.infer/reset`; `ActionChunkBroker` replays chunks open-loop with **no staleness check**; websocket policy with a **metadata handshake**, but blocking `recv()` with no timeout and retries forever; wall-clock pacing |
| dora-rs | Apache-2.0; 1.0 announced 2026-09 | uhlc **hybrid-logical-clock** timestamps on every message, plus an `open_telemetry_context` key; patterns: `request_id` (UUID v7, pass-through required), `goal_id` with `goal_status`, cancel; inputs with `queue_policy` (drop_oldest/backpressure) and `input_timeout`; `MockNode` testing fake; record/replay; restart policies; CI **breaking-change gate** over the YAML, wire and Python surface |
| Auromix ROS-LLM | Apache-2.0; dead since 2023 | `getattr(self, function_name)` on model output with unbounded floats. Anti-pattern only. |
| **PhyAgentOS** (Sun Yat-sen Univ.) | MIT; active, v1.1 | `verification/contracts.py`: `TaskVerificationContract` modes off/audit/enforce/recovery; **`ExecutionRecord` = "immutable facts…never a task verdict"** with status `unknown`; `EvidenceArtifact` phase before/during/after and **association_quality best_effort|authoritative**; verdict success/failure/replan_required/inconclusive. `forge/task.py` refuses to resend "an identical Action [with] unknown remote state; reconcile it instead", and `reconcile_nonterminal()` repairs from status reads without re-dispatching. `agent/experience/contracts.py` has lessons with guarded promotion. **But the verdict engine is a VLM**, and shell/filesystem tools sit next to robot skills. |
| OpenLoong (AtomGit) | 34 repos with no license metadata; OpenLoongBrain has no LICENSE | LLM intent class calls `t_action_grasp_all` directly, then `time.sleep(40)` as "done", inside `while True: try/except: pass`. Anti-pattern; do not copy code. |
| AimRT (AgiBot) | **Mulan PSL v2** (AGPL compatibility [U]) | middleware; ideas only, pending legal review |
| Alibaba RynnRCP | Apache-2.0 | robot capability abstraction; `RobotMotion` converts low-rate inference to high-rate control (README only) |
| Unitree unitree_sdk2_python | BSD-3 | `SportClient` fire-and-forget `Move`; **`LeaseClient` exclusive control lease, off by default** |
| LangGraph | MIT; very active | `BaseCheckpointSaver.put_writes(task_id)` (pending writes avoid re-running completed tasks); `interrupt()` and `Command(resume=…)` need a checkpointer; nodes restart from the beginning on resume, so side effects before an interrupt must be idempotent; resume values are matched **by index** (fragile) |
| Gitee | search API needs auth; only a RAI mirror found | - |
| MindSpore robotics agent | none found | - |

## Adoption items

| # | Pattern | AIR module | REQ | P | Effort |
|---|---|---|---|---|---|
| 1 | **Durable command intent journal**: write the intent before dispatch; rebuild handles on restart; an intent without a terminal state becomes UNRESOLVED and is reconciled by observation, never resent | `runtime/runtime.py`, new `runtime/journal.py` | REQ14, REQ07 | **P0** | M |
| 2 | **Execution facts separate from the verdict**: an adapter report cannot claim VERIFIED; evidence carries phase and association quality; only authoritative evidence satisfies postconditions | `contracts/execution.py`, runtime `_verify` | REQ14 | **P0** | S-M |
| 3 | `SkillSpec.occupies=("base","arm")` turns into token-scoped holds at admission; the structured rejection names the holder | `contracts/skill.py`, `resources.py`, `admission.py` | REQ14, REQ05 | P1 | S |
| 4 | **Human approval as a first-class interrupt**: `AWAITING_APPROVAL` state; `resume(command_id, decision, approver, authority_revision)` bound by id, not position; idempotent pre-approval code | `admission.py`, `contracts/authority.py`, CommandState | REQ17, REQ07 | **P0** | M |
| 5 | **Report the applied command, not the requested one**; trace clamping; typed `max_relative_target` and workspace-box params that reject NaN | `contracts/skill.py`, `plugins.py`, runtime trace | REQ14, REQ12 | P1 | S |
| 6 | Action-chunk policy contract with a **staleness bound** and drop-queue-on-interrupt; policies sit behind the SkillSpec boundary | new `contracts/policy.py` | REQ11, REQ12 | P1 | M |
| 7 | LeRobot and openpi optional adapters (with timeouts and a reconnect limit) | `integrations/` | REQ01, REQ11 | P1 | M |
| 8 | Version handshake on connect, recorded as an entity; a change bumps the capability revision | `plugins.py`, provenance | REQ11, REQ14 | P2 | S |
| 9 | Correlation metadata on the wire (request_id/goal_id UUID v7, pass-through, goal_status, cancel); **HLC timestamps** for cross-robot ordering | `coordination/protocol.py`, `transport.py`, `core/clock.py` | REQ12, REQ14 | P1 | M |
| 10 | dora transport integration | `integrations/dora.py` | REQ12, REQ01 | P2 | M |
| 11 | The fake adapter implements every protocol method (unsupported ones raise); CI breaking-change gate on contract schemas | `testing/`, `conformance.py`, CI | REQ18 | P1 | S |
| 12 | **MCP server exposing AIR skills as proposals**: `tools/call` goes to `Runtime.submit` with a session delegation and returns handle state, never "success" | `integrations/mcp.py` | REQ05, REQ18 | P2 | M |
| 13 | Bounded context aggregators per source with freshness labels | `agents/context.py` | REQ08 | P2 | S |
| 14 | Experience-to-lesson pipeline with guarded promotion | provenance, agents | REQ11, REQ08 | P2 | L |
| 15 | Conversation identity (communication_id, seq_no, seq_end) cited by commitments | agents, contracts | REQ07 | P1 | S |
| 16 | Adapters must hold vendor control leases (e.g. Unitree); losing the lease downgrades or revokes capability | adapter guidelines, conformance | REQ17, REQ14 | P2 | S |

## Anti-patterns

1. LLM tool calls that actuate directly (ROSA shell, ROS-LLM getattr, OpenLoong)
2. allow-by-default permissions
3. guardrails that live only in the prompt
4. **an LLM/VLM as the judge of physical success**. At most it is non-authoritative
   evidence that can move a command to UNRESOLVED or trigger a replan; it never sets
   VERIFIED.
5. elapsed time treated as completion; no timeouts; wall-clock pacing
6. silent infinite retry loops
7. open-loop replay of stale action chunks
8. plugin discovery by import side effects
9. shared mutable module state
10. shell/filesystem tools in the same scope as actuation
11. a framework dependency in the core
