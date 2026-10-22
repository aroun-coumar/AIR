# 0008. Agentic engineering work in sandboxes, with provider-agnostic models

Status: Accepted
Date: 2026-10-22

## Context
The owner set the scope: AIR is not only agentic robotics. Research, development,
simulation and maintenance should be done through agents wherever useful, in a sandbox
or on a platform. On models: provider-agnostic but built accordingly, with both local
open models and closed APIs, and a future where robots host their own intelligence.
Baseline section 8 names the roles: engineering, diagnostic, knowledge curator,
maintenance planner. Requirements AIR01–AIR03 were added to the catalog.

## Decisions
1. **Model layer** (`agents/providers/`). A `ModelProfile` records *where* a model runs
   (`cloud`, `edge`, `local_host`, `on_robot`) and *whether data leaves the device*.
   - `OpenAICompatibleProvider` (stdlib HTTP) covers Ollama, vLLM, llama.cpp, LM Studio
     and other compatible servers, including on-robot endpoints. Models without tool
     calling use a JSON text protocol.
   - `AnthropicProvider` uses the official SDK as an optional extra. Default
     `claude-opus-5-5` with explicit `high` effort, thinking left at its default (always
     on), `tool_choice` left at `auto`, `fallbacks: "default"` for refusals, refusals
     checked before content, and the native transcript appended unchanged.
   - `ModelRouter` and `ModelPolicy` choose models by placement and egress. The order is
     this robot, this host, edge, then cloud, unless `prefer` says otherwise. The router
     falls back only on the first turn, then pins. Every call is traced (model,
     location, egress, tokens, latency), never content.
   - Robots advertise self-hosted models through `RobotAdapter.hosted_models()`.
   - `.air/models.json` (`ar.model_catalog/1.0`) declares a project's models. Keys are
     referenced by environment-variable name only.
2. **Sandboxes** (`work/sandbox.py`). Each sandbox *declares* what it enforces; a
   policy can require controls, and work is refused if they can't be enforced.
   - `LocalProcessSandbox`: private workspace, no shell, a command allowlist, a scrubbed
     environment (no credentials), timeouts, output caps, isolated-mode Python; memory
     and CPU limits on POSIX. It does **not** jail the filesystem or the network.
   - `ContainerSandbox`: Docker or Podman with `--network none`, resource limits, a
     read-only root and dropped capabilities.
   - `default_sandbox` picks a container runtime when one is installed.
3. **Work agents** (`work/`).
   - A `WorkTask` (`ar.work_task/1.0`) covers research, development, simulation,
     evaluation, maintenance or diagnosis, with requirements, inputs, acceptance
     criteria, a budget, a sandbox policy, model constraints and a data classification.
     `restricted` data never reaches a model where data leaves the device.
   - Typed tools declare a side effect (read, workspace, compute, project, external,
     robot, promote). Each call goes through the tool allowlist, argument validation,
     authority (operation `work`) and, for consequential side effects, approval
     (default deny); then the sandbox and the ledger.
   - There is no direct actuation tool. `request_robot_skill` goes through Runtime
     admission.
4. **Proposals, not changes.** Agents deliver `ar.work_proposal/1.0` with
   content-addressed artifacts. `decide()` needs a different principal (and, with an
   authority service, the operation `promote`) and records a decision activity. A
   maintenance *plan* is never recorded as maintenance done.
5. **Everything is evidence.** Each run is a provenance activity of the matching kind
   (the new `diagnosis` kind takes Activity to 1.1). It records the sandbox's real
   isolation, every model turn and tool call (argument digests, not content),
   approvals, findings and proposals, all on one trace.

## Consequences
- The local process sandbox is suitable for trusted development. Sensitive or
  untrusted work should set `require_enforced: [network, filesystem]`, which only a
  container (or a future VM or remote sandbox) satisfies.
- The container image must contain AIR for tools such as `run_scenario`; the default
  `python:3.12-slim` image only runs plain Python. Not yet built here, because Docker is
  not installed on the development machine.
- Mid-conversation model failover is not attempted, because transcripts are
  provider-native. A failed task can be re-run under a different policy.
- Remote sandbox platforms (E2B, Modal, Daytona) and a VM sandbox can implement the same
  `Sandbox` protocol later.
