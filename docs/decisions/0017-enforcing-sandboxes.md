# 0017. Enforcing sandboxes and agent tool policy

Status: Accepted
Date: 2026-10-22

## Context
ADR 0008 gave work agents a sandbox that *declares* what it enforces. On a laptop that
was mostly a `LocalProcessSandbox`: no filesystem jail, no network isolation, and on
Windows no memory limit. The approval path asked a single approver, bound nothing to the
arguments, and could not run unattended safely. The catalog backlog (Wave 4: W4.1 to
W4.6) and the W2.5 approval amendments, collected from the surveyed sandboxes and agent
frameworks, describe what should change. The anti-pattern charter applies: no
allow-by-default, no guardrails only in a prompt, and nothing a model says grants
authority.

## Decisions
1. **Enforcing local adapters (W4.1).** Every adapter declares exactly what it
   enforces, and runs external tools through an injectable `ProcessRunner`, so its
   command construction is unit-tested with a fake runner. Real runs skip when the tool
   is absent.
   - `BubblewrapSandbox` (Linux): `--ro-bind / /`, `--bind` for the workspace, protected
     paths (default `.git`) re-bound read-only, or `/dev/null` mounted on them when
     missing (the codex linux-sandbox approach), `--unshare-all` (network included
     unless `network: any`), `--die-with-parent`, `--new-session`, `--clearenv` with
     explicit `--setenv`, and `deny_read` paths masked. No seccomp filter is loaded.
     The capability detail says so.
   - `LandlockSandbox` (Linux, `landrun`): Landlock filesystem rules. The ABI is probed
     with `landlock_create_ruleset(..., LANDLOCK_CREATE_RULESET_VERSION)` through ctypes
     and recorded. Network is enforced only with ABI ≥ 10 (TCP and UDP). Proxy mode is
     by port, not host, and is never counted as enforced. Protected paths inside the
     writable workspace cannot be re-protected under Landlock, so they are reported as
     not enforced.
   - `SeatbeltSandbox` (macOS): `seatbelt_profile()` generates a deny-by-default SBPL
     profile. Writes are allowed only to the workspace and `/dev/null`, protected paths
     are denied after that allow (later rules win), and network is none, any, or only
     `localhost:<proxy port>`. Memory is not claimed, because macOS ignores `RLIMIT_AS`.
     It is untested on real macOS here; the real-run test skips.
   - Windows: `LocalProcessSandbox` now enforces **memory and CPU** through a Job Object
     (`work/winjob.py`, stdlib `ctypes`):
     - `JOB_OBJECT_LIMIT_JOB_MEMORY`;
     - a job CPU-time limit of `max_wall_s × max_cpus`;
     - a hard CPU-rate cap;
     - `KILL_ON_JOB_CLOSE`, so descendants die when the run ends or times out.

     The process is created suspended, assigned, then resumed, so no child escapes. The
     filesystem and network are **still not enforced** on Windows: that needs a
     dedicated user, ACLs and a WFP egress filter, which is the sandbox-runtime design
     and needs administrator setup. Tested for real on this host: a 600 MiB allocation
     under a 200 MiB limit fails.
   - `default_sandbox` tries the container, bubblewrap, Seatbelt and Landlock adapters
     in that order, then falls back to local. `air sandbox probe` shows what each one
     enforces on this host.
2. **Egress policy as data (W4.2).** `SandboxPolicy` 1.1 adds `network: proxy` and
   `egress: EgressPolicy`. `EgressProxy` is a stdlib HTTP forward proxy with `CONNECT`.
   - Private, CGNAT, ULA, loopback, link-local, multicast, reserved and unspecified
     addresses, plus the cloud metadata endpoints, are denied by default. Metadata
     addresses are also checked through IPv4-mapped and NAT64 forms.
   - Explicit `allow_cidrs` rules may open a range. A metadata address opens only for a
     rule naming exactly that address.
   - Allowed domains are resolved and pinned when the proxy starts (wildcards on first
     use), and never re-resolved. The proxy dials only the pinned addresses, and answers
     in denied ranges are dropped, which protects against DNS rebinding.
   - Secrets are `SecretBinding`s. The sandbox environment gets a random placeholder;
     the real value comes from a named host variable and stays in proxy memory. It is
     substituted only when all of these hold:
     - the request is visible to the proxy (absolute-form HTTP);
     - the host is bound to the secret;
     - the placeholder is in an allowed header;
     - the proxy itself opens TLS to the upstream (`tls_origination`), unless the
       binding opts into plain HTTP.

     A placeholder seen anywhere else blocks the request. Values echoed back are
     redacted. `CONNECT` tunnels are opaque and never receive substitutions.
   - Every decision is an event, and each `RunResult` carries the events and violations
     of *that* command. Under bubblewrap, the network namespace is empty and the only way
     out is `netbridge.py`, which bridges a loopback port to the proxy's bind-mounted
     Unix socket. Under the local adapter the proxy is advisory, so `network` stays
     unenforced. Tested locally on 127.0.0.1 with a fake upstream.
3. **Strict vs best effort (W4.3).** `mode: strict` (default) refuses when a
   `require_enforced` control is not enforced. `best_effort` runs, and every result's
   `not_applied` lists the missing controls. `applied` lists the concrete mechanisms
   (including the Landlock ABI and job limits). The ledger's `work.sandbox` entry records
   mode, placement, applied, not applied and ABI.
4. **Remote and microVM sandboxes (W4.4).** `RemoteSandbox` runs over a
   `RemoteSandboxClient` protocol (create, write, read, files, exec, destroy). This keeps
   orchestration separate from isolation, as kubernetes-sigs/agent-sandbox does.
   Placement (`remote` or `cloud`) and `data_leaves_device` are recorded, and the work
   agent refuses `restricted` tasks on such sandboxes. `FakeRemoteClient` is the
   reference. No E2B adapter was written: its current REST and SDK surface was not
   verified from source in this pass, and it needs an account.
5. **Command policy rules (W4.5).** `ar.command_policy/1.0`, following codex execpolicy:
   - prefix rules whose tokens may list alternatives;
   - `allow`, `prompt` or `forbidden`, with a justification;
   - `match` / `not_match` examples checked at load time;
   - the strictest matching rule wins;
   - program names are normalized (basename, case, `.exe`).

   The built-in rules forbid `git push` and package installs, and prompt for network
   clients and history rewrites. A task's rules are *added*, so a task can tighten the
   built-in rules but never loosen them. They apply to the `run` tool. The unmatched
   default is `allow`, meaning fall back to the sandbox, whose command allowlist still
   denies by default. `air work rules check|validate` exposes this.
6. **Risk separate from confirmation (W4.6).** Three analyzers each score a call:
   - side effect;
   - command policy;
   - pattern: privilege escalation, `curl | sh`, metadata, credential files,
     `.git/hooks`, and so on.

   The worst score wins: HIGH, then UNKNOWN, then the highest known. An analyzer that
   fails or cannot inspect a call yields UNKNOWN. `ConfirmRisky` (threshold from
   `WorkTask.confirm_risk`, default HIGH, never UNKNOWN) asks at or above the threshold
   and on UNKNOWN. Risk only adds asks: declared side effects in `approval_required`
   remain the floor.
7. **Approval hardening (W2.5).**
   - `ApprovalRequest` carries the call id, tool, exact argument digest, reasons, risk
     and a bypass-immune flag. A `Decision` that names another call, tool or digest is a
     rejection.
   - A pre-issued `Grant` is honoured once, and only for the matching pending call. A
     call id that has been put to approval cannot run again.
   - Unparseable arguments (`__invalid_json__`) always need approval and never run as
     given. An approver may supply edited arguments for the same tool; they are
     re-validated and re-checked against the command policy, and the model is told
     what ran.
   - Approver chains approve, reject or escalate. If nobody decides, the call is
     rejected and the steps are recorded.
   - `dont_ask` (`WorkTask.approval_mode`, `WorkAgent(unattended=True)`, or
     `air work run --unattended`) turns every ask into a deny. `AllowRule`s silence only
     asks that are not bypass-immune. Robot and promote effects, high or unknown risk,
     unparseable arguments and command-policy prompts are bypass-immune.
8. **Contracts.**
   - `sandbox_policy` 1.0 → 1.1 adds `mode`, `egress`, `protected_paths` and
     `deny_read`, plus the `proxy` network mode.
   - `work_task` 1.0 → 1.1 adds `command_policy`, `confirm_risk` and `approval_mode`.
   - `command_policy` 1.0 is new.

   All new fields have defaults; 1.0 documents still load, and unknown fields still
   fail. `CONTROLS` gains `protected_paths`.

## Consequences
- On Linux with bubblewrap, or on macOS, a policy can now *require* `filesystem`,
  `network` and `protected_paths` and get them. On Windows, memory and CPU are now real
  controls, but filesystem and network still are not. Strict policies that require them
  are refused there, as before.
- Tests that use the scripted model with repeated default call ids are unaffected: the
  single-use rule applies to calls that went to approval.
- Seatbelt, bubblewrap and Landlock real runs were not executed on this Windows host.
  Their command lines are tested; the real-run tests skip.
- `default_sandbox` may now pick bubblewrap, Seatbelt or Landlock where they are
  installed, so evidence on such hosts will show stronger isolation than before.

## Sources (patterns adopted; no code copied)
| Source | License | Used for |
|---|---|---|
| anthropics/sandbox-runtime (README) | Apache-2.0 | deny-read / allow-write model, proxy-only egress, Unix-socket bridge on Linux, Windows design (dedicated user + WFP) |
| openai/codex `codex-rs/execpolicy/README.md` | Apache-2.0 | prefix rules, allow/prompt/forbidden, justification, load-time examples, strictest wins |
| openai/codex `codex-rs/linux-sandbox/README.md` | Apache-2.0 | `--ro-bind / /`, writable roots, `.git` re-bound read-only, `/dev/null` on missing protected paths, `--unshare-net` with a proxy bridge |
| openai/codex `codex-rs/sandboxing/src/seatbelt_*.sbpl` | Apache-2.0 | shape of the deny-default Seatbelt profile |
| containers/bubblewrap `bwrap.xml` | LGPL-2.1 (invoked as a binary) | flag semantics (`--unshare-all`, `--share-net`, `--clearenv`, `--die-with-parent`, `--ro-bind-try`) |
| zouuup/landrun README | MIT | strict vs `--best-effort`, Landlock ABI table, port-only network, MPTCP and pre-opened fd gaps |
| google/nsjail `config.proto` | Apache-2.0 | control checklist (time, rlimits, cgroups, clone_newnet, keep_env=false, mandatory mounts) |
| superradcompany/microsandbox `docs/security/network.mdx`, `secrets.mdx` | Apache-2.0 | denied ranges, DNS pinning, rebinding, placeholder secrets substituted only for allowed hosts and headers |
| OpenHands software-agent-sdk `security/confirmation_policy.py` | MIT | risk levels, ConfirmRisky, threshold ≠ UNKNOWN |
| google/adk-python, openai-agents-python, langchain HITL, inspect_ai, agentscope | Apache-2.0 / MIT | bound single-use approvals, unparseable → approval, edits re-validated, escalate chains, default reject, `dont_ask`, bypass-immune |
| kubernetes-sigs/agent-sandbox | Apache-2.0 | orchestration separate from isolation |

## Not done / needs the owner
- **Windows filesystem and network enforcement.** This needs a dedicated low-privilege
  account, NTFS ACLs and a Windows Filtering Platform egress filter. It requires
  administrator setup and a decision on installing a service.
- **Seccomp for bubblewrap.** Generating cBPF in the stdlib is possible but error-prone.
  An optional extra that ships a vetted filter (e.g. via `--seccomp FD`) is the
  suggested follow-up.
- **TLS interception** for secret substitution inside `CONNECT` tunnels. It is
  deliberately not done: it would require a sandbox-trusted CA.
- **Provider adapters for E2B, microsandbox and Daytona.** These need accounts or
  services, and verification of their current APIs. Daytona's public repository is
  frozen (AGPL), so it would be remote-only.
- Real-run verification of the bubblewrap, Landlock and Seatbelt adapters on Linux and
  macOS hosts (CI matrix).
- A model-based risk analyzer, and per-command violation attribution for filesystem
  denials (only egress is attributed today).
