"""Remote and microVM sandboxes: one adapter, many providers.

:class:`RemoteSandbox` implements the :class:`~agentic_robotics.work.sandbox.Sandbox`
protocol over a :class:`RemoteSandboxClient`. The client is the *orchestration* side
(create, copy files, execute, destroy); the *isolation* (Firecracker microVM, gVisor,
Kata, a container) is the provider's, and the client declares which controls it
enforces. AIR keeps its own capability record and records the placement (``remote`` or
``cloud``) and that workspace data leaves the device, so restricted work is never sent
there (the work agent refuses it). This separation follows kubernetes-sigs/agent-sandbox
(Apache-2.0).

The workspace is mirrored locally: files are written locally, pushed before each run and
pulled back after it, so tools keep the synchronous workspace API.

Provider adapters (E2B, microsandbox, Daytona) need accounts or services this repository
cannot verify offline; :class:`FakeRemoteClient` is the reference implementation used
by the tests.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .sandbox import (
    EnforcementMode,
    Isolation,
    Placement,
    RunResult,
    SandboxCapabilities,
    SandboxError,
    SandboxPolicy,
    _WorkspaceMixin,
    policy_problems,
)

__requirements__ = ("AIR01", "REQ02", "REQ04", "G15", "F07")


@dataclass(frozen=True)
class RemoteExec:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool = False


class RemoteSandboxClient(Protocol):
    """What a remote/microVM provider must offer. All calls may block (network I/O);
    :class:`RemoteSandbox` runs them off the event loop."""

    provider: str
    placement: Placement
    isolation: Isolation

    def enforced(self, policy: SandboxPolicy) -> frozenset[str]: ...

    def create(self, policy: SandboxPolicy) -> str: ...

    def write(self, session: str, rel: str, data: bytes) -> None: ...

    def read(self, session: str, rel: str) -> bytes: ...

    def files(self, session: str) -> list[str]: ...

    def exec(self, session: str, argv: list[str], env: dict[str, str],
             timeout_s: float) -> RemoteExec: ...

    def destroy(self, session: str) -> None: ...


class RemoteSandbox(_WorkspaceMixin):
    name = "remote"

    def __init__(self, client: RemoteSandboxClient, policy: SandboxPolicy | None = None, *,
                 root: str | Path | None = None) -> None:
        self.client = client
        self.policy = policy or SandboxPolicy()
        self.capabilities = SandboxCapabilities(
            client.isolation, client.enforced(self.policy),
            f"{client.provider} ({client.placement.value})", placement=client.placement,
            applied=(f"provider:{client.provider}",), data_leaves_device=True)
        problems = policy_problems(self.policy, self.capabilities)
        if problems and self.policy.mode is EnforcementMode.STRICT:
            raise SandboxError("sandbox cannot honour the policy", problems=problems,
                               sandbox=client.provider)
        self.not_applied = [c for c in self.policy.require_enforced
                            if c not in self.capabilities.enforced]
        self.workspace = Path(tempfile.mkdtemp(prefix="air-remote-", dir=root)).resolve()
        self.session: str | None = None

    def _push(self) -> None:
        assert self.session is not None
        for rel in super().list_files():
            self.client.write(self.session, rel, (self.workspace / rel).read_bytes())

    def _pull(self) -> None:
        assert self.session is not None
        for rel in self.client.files(self.session):
            target = self.path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.client.read(self.session, rel))

    def _allowed(self, argv: list[str]) -> None:
        if not argv:
            raise SandboxError("empty command")
        name = Path(argv[0]).name.lower().removesuffix(".exe")
        if name not in {c.lower() for c in self.policy.allowed_commands}:
            raise SandboxError("command not allowed by the sandbox policy", command=argv[0],
                               allowed=list(self.policy.allowed_commands))

    def _sync_run(self, argv: list[str], timeout: float) -> RemoteExec:
        if self.session is None:
            self.session = self.client.create(self.policy)
        self._push()
        env = {k: os.environ[k] for k in self.policy.env_allowlist if k in os.environ}
        result = self.client.exec(self.session, argv, env, timeout)
        self._pull()
        return result

    async def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult:
        self._allowed(list(argv))
        timeout = min(timeout_s or self.policy.max_wall_s, self.policy.max_wall_s)
        started = time.monotonic()
        r = await asyncio.to_thread(self._sync_run, list(argv), timeout)
        limit = self.policy.max_output_bytes
        truncated = len(r.stdout) > limit or len(r.stderr) > limit
        return RunResult(argv=list(argv), exit_code=r.exit_code, stdout=r.stdout[:limit],
                         stderr=r.stderr[:limit], duration_s=round(time.monotonic() - started, 3),
                         timed_out=r.timed_out, truncated=truncated,
                         unenforced=self.capabilities.unenforced(),
                         not_applied=list(self.not_applied),
                         applied=list(self.capabilities.applied),
                         placement=self.capabilities.placement)

    def close(self) -> None:
        if self.session is not None:
            self.client.destroy(self.session)
            self.session = None
        shutil.rmtree(self.workspace, ignore_errors=True)


@dataclass
class FakeRemoteClient:
    """A reference provider: each session is a private directory and commands run as
    local subprocesses. It stands in for a provider in tests; it is *not* isolation, so
    it declares only the controls it really applies."""

    provider: str = "fake-remote"
    placement: Placement = Placement.CLOUD
    isolation: Isolation = Isolation.VM
    claims: frozenset[str] = frozenset({"time", "env", "output"})
    sessions: dict[str, Path] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def enforced(self, policy: SandboxPolicy) -> frozenset[str]:
        return self.claims

    def create(self, policy: SandboxPolicy) -> str:
        sid = f"fake-{len(self.sessions) + 1}"
        self.sessions[sid] = Path(tempfile.mkdtemp(prefix="air-fake-remote-"))
        self.calls.append(f"create {sid}")
        return sid

    def _p(self, session: str, rel: str) -> Path:
        root = self.sessions[session]
        p = (root / rel).resolve()
        if not p.is_relative_to(root.resolve()):
            raise SandboxError("path escapes the remote workspace", path=rel)
        return p

    def write(self, session: str, rel: str, data: bytes) -> None:
        p = self._p(session, rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read(self, session: str, rel: str) -> bytes:
        return self._p(session, rel).read_bytes()

    def files(self, session: str) -> list[str]:
        root = self.sessions[session].resolve()
        return sorted(p.resolve().relative_to(root).as_posix() for p in root.rglob("*")
                      if p.is_file())

    def exec(self, session: str, argv: list[str], env: dict[str, str],
             timeout_s: float) -> RemoteExec:
        self.calls.append("exec " + " ".join(argv))
        cmd = [sys.executable, "-I", "-B", *argv[1:]] if argv[0] in ("python", "python3") \
            else argv
        base = {k: os.environ[k] for k in ("SYSTEMROOT", "PATH") if k in os.environ}
        try:
            p = subprocess.run(cmd, cwd=self.sessions[session], env={**base, **env},
                               capture_output=True, timeout=timeout_s, check=False)
        except subprocess.TimeoutExpired:
            return RemoteExec(None, "", "", True)
        return RemoteExec(p.returncode, p.stdout.decode("utf-8", "replace"),
                          p.stderr.decode("utf-8", "replace"))

    def destroy(self, session: str) -> None:
        self.calls.append(f"destroy {session}")
        shutil.rmtree(self.sessions.pop(session), ignore_errors=True)


__all__ = ["FakeRemoteClient", "RemoteExec", "RemoteSandbox", "RemoteSandboxClient"]
