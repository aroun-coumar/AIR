"""Sandboxes where agent work executes: research scripts, development builds and tests,
simulations, analyses.

A sandbox declares what it actually enforces (:class:`SandboxCapabilities`). A task's
:class:`SandboxPolicy` can require particular controls to be enforced; if the sandbox
cannot enforce them, the work is refused instead of running with weaker isolation than it
claims. Unenforced controls are recorded with every run, so the evidence states the real
isolation.

* :class:`LocalProcessSandbox`: always available. Private temporary workspace, no
  shell, executable allowlist, scrubbed environment (no credentials), wall-clock
  timeout with kill, output caps, Python in isolated mode. It does **not** enforce a
  filesystem jail or network isolation, and enforces memory limits only on POSIX.
* :class:`ContainerSandbox`: Docker or Podman, when installed. ``--network none``,
  memory/CPU/PID limits, read-only root, dropped capabilities, workspace bind mount.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from ..core.errors import FrameworkError
from ..core.schema import Contract, contract

__requirements__ = ("AIR01", "REQ02", "REQ04", "G15")

CONTROLS = ("filesystem", "network", "memory", "cpu", "time", "env", "output")


class SandboxError(FrameworkError):
    code = "sandbox_error"


class Isolation(StrEnum):
    PROCESS = "process"
    CONTAINER = "container"
    VM = "vm"
    REMOTE = "remote"


@dataclass(frozen=True)
class SandboxCapabilities:
    isolation: Isolation
    enforced: frozenset[str]          # subset of CONTROLS actually enforced
    detail: str = ""

    def unenforced(self) -> list[str]:
        return [c for c in CONTROLS if c not in self.enforced]


class NetworkMode(StrEnum):
    NONE = "none"
    ANY = "any"


@contract("sandbox_policy", version="1.0")
class SandboxPolicy(Contract):
    """What work in the sandbox may do, and which limits must be *enforced*."""

    network: NetworkMode = NetworkMode.NONE
    allowed_commands: tuple[str, ...] = ("python",)
    env_allowlist: tuple[str, ...] = ()
    max_wall_s: float = 120.0
    max_memory_mb: int | None = 2048
    max_cpus: float | None = 2.0
    max_output_bytes: int = 200_000
    require_enforced: tuple[str, ...] = ("time", "env", "output")

    def __post_init__(self) -> None:
        unknown = set(self.require_enforced) - set(CONTROLS)
        if unknown:
            raise SandboxError("unknown sandbox controls", unknown=sorted(unknown))


def policy_problems(policy: SandboxPolicy, caps: SandboxCapabilities) -> list[str]:
    """Why ``policy`` cannot be honoured by a sandbox with ``caps`` (empty if it can)."""
    return [f"policy requires '{c}' to be enforced, but the {caps.isolation.value} "
            f"sandbox does not enforce it" for c in policy.require_enforced
            if c not in caps.enforced]


@dataclass
class RunResult:
    argv: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False
    truncated: bool = False
    unenforced: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class Sandbox(Protocol):
    capabilities: SandboxCapabilities
    policy: SandboxPolicy
    workspace: Path

    async def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult: ...

    def path(self, rel: str) -> Path: ...

    def read_text(self, rel: str, limit: int = 200_000) -> str: ...

    def write_text(self, rel: str, text: str) -> Path: ...

    def import_file(self, source: Path, rel: str) -> Path: ...

    def list_files(self, rel: str = ".") -> list[str]: ...

    def close(self) -> None: ...


class _WorkspaceMixin:
    workspace: Path

    def path(self, rel: str) -> Path:
        """Resolve a workspace-relative path; refuse anything outside the workspace."""
        if not rel or Path(rel).is_absolute() or Path(rel).drive:
            raise SandboxError("paths must be relative to the workspace", path=rel)
        target = (self.workspace / rel).resolve()
        if not target.is_relative_to(self.workspace.resolve()):
            raise SandboxError("path escapes the workspace", path=rel)
        return target

    def read_text(self, rel: str, limit: int = 200_000) -> str:
        data = self.path(rel).read_bytes()[:limit]
        return data.decode("utf-8", "replace")

    def write_text(self, rel: str, text: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def import_file(self, source: Path, rel: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, p)
        return p

    def list_files(self, rel: str = ".") -> list[str]:
        base = self.path(rel) if rel not in (".", "") else self.workspace.resolve()
        if not base.exists():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and ".air-tmp" not in p.parts:
                out.append(p.resolve().relative_to(self.workspace.resolve()).as_posix())
        return out


async def _collect(proc: asyncio.subprocess.Process, timeout_s: float,
                   limit: int) -> tuple[bytes, bytes, bool, bool]:
    async def read(stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        buf = bytearray()
        truncated = False
        while chunk := await stream.read(65536):
            room = limit - len(buf)
            if room > 0:
                buf += chunk[:room]
            if len(chunk) > max(room, 0):
                truncated = True
        return bytes(buf), truncated

    try:
        (out, t1), (err, t2) = await asyncio.wait_for(
            asyncio.gather(read(proc.stdout), read(proc.stderr)), timeout_s)
        await asyncio.wait_for(proc.wait(), timeout_s)
        return out, err, t1 or t2, False
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return b"", b"", False, True


class LocalProcessSandbox(_WorkspaceMixin):
    def __init__(self, policy: SandboxPolicy | None = None,
                 root: str | Path | None = None) -> None:
        self.policy = policy or SandboxPolicy()
        enforced = {"time", "env", "output"}
        if os.name == "posix":
            enforced |= {"memory", "cpu"}
        self.capabilities = SandboxCapabilities(
            Isolation.PROCESS, frozenset(enforced),
            "private workspace, no shell, command allowlist, scrubbed env, timeouts; "
            "no filesystem jail or network isolation")
        problems = policy_problems(self.policy, self.capabilities)
        if problems:
            raise SandboxError("sandbox cannot honour the policy", problems=problems)
        self.workspace = Path(tempfile.mkdtemp(prefix="air-sbx-", dir=root))
        (self.workspace / ".air-tmp").mkdir()

    def _env(self) -> dict[str, str]:
        tmp = str(self.workspace / ".air-tmp")
        env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent)]),
               "TMP": tmp, "TEMP": tmp, "TMPDIR": tmp, "HOME": str(self.workspace),
               "USERPROFILE": str(self.workspace), "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        if os.name == "nt":
            for key in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
                if key in os.environ:
                    env[key] = os.environ[key]
        for key in self.policy.env_allowlist:
            if key in os.environ:
                env[key] = os.environ[key]
        return env

    def _resolve(self, argv: list[str]) -> list[str]:
        if not argv:
            raise SandboxError("empty command")
        name = Path(argv[0]).name.lower().removesuffix(".exe")
        if name not in {c.lower() for c in self.policy.allowed_commands}:
            raise SandboxError("command not allowed by the sandbox policy", command=argv[0],
                               allowed=list(self.policy.allowed_commands))
        if name in ("python", "python3"):
            # The framework's own interpreter, isolated from user site and env vars.
            return [sys.executable, "-I", "-B", *argv[1:]]
        exe = shutil.which(argv[0])
        if exe is None:
            raise SandboxError("command not found", command=argv[0])
        return [exe, *argv[1:]]

    def _limits(self) -> object | None:
        if sys.platform == "win32":
            return None
        import resource

        mem = self.policy.max_memory_mb
        cpus = self.policy.max_cpus
        wall = self.policy.max_wall_s

        def apply() -> None:  # runs in the child before exec
            if mem:
                resource.setrlimit(resource.RLIMIT_AS, (mem * 2**20, mem * 2**20))
            if cpus:
                cpu_s = int(wall * cpus) + 1
                resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        return apply

    async def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult:
        cmd = self._resolve(list(argv))
        timeout = min(timeout_s or self.policy.max_wall_s, self.policy.max_wall_s)
        started = time.monotonic()
        kwargs: dict[str, object] = {}
        limits = self._limits()
        if limits is not None:
            kwargs["preexec_fn"] = limits
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(self.workspace), env=self._env(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)  # type: ignore[arg-type]
        out, err, truncated, timed_out = await _collect(proc, timeout,
                                                        self.policy.max_output_bytes)
        return RunResult(argv=list(argv), exit_code=None if timed_out else proc.returncode,
                         stdout=out.decode("utf-8", "replace"),
                         stderr=err.decode("utf-8", "replace"),
                         duration_s=round(time.monotonic() - started, 3), timed_out=timed_out,
                         truncated=truncated, unenforced=self.capabilities.unenforced())

    def close(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


class ContainerSandbox(_WorkspaceMixin):
    """Docker/Podman sandbox. Network isolation, resource limits and a read-only root
    are enforced by the container runtime."""

    def __init__(self, policy: SandboxPolicy | None = None, *, image: str = "python:3.12-slim",
                 runtime: str | None = None, root: str | Path | None = None) -> None:
        self.policy = policy or SandboxPolicy()
        self.image = image
        self.runtime = runtime or self.detect_runtime()
        if self.runtime is None:
            raise SandboxError("no container runtime (docker or podman) is installed")
        self.capabilities = SandboxCapabilities(Isolation.CONTAINER, frozenset(CONTROLS),
                                                f"{Path(self.runtime).name}: {image}")
        self.workspace = Path(tempfile.mkdtemp(prefix="air-sbx-", dir=root))

    @staticmethod
    def detect_runtime() -> str | None:
        return shutil.which("docker") or shutil.which("podman")

    def container_argv(self, argv: list[str]) -> list[str]:
        name = Path(argv[0]).name.lower() if argv else ""
        if name not in {c.lower() for c in self.policy.allowed_commands}:
            raise SandboxError("command not allowed by the sandbox policy", command=argv[0]
                               if argv else "", allowed=list(self.policy.allowed_commands))
        cmd = [str(self.runtime), "run", "--rm", "--init",
               "--network", "none" if self.policy.network is NetworkMode.NONE else "bridge",
               "--read-only", "--tmpfs", "/tmp:rw,size=256m", "--pids-limit", "256",
               "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
               "-v", f"{self.workspace}:/workspace:rw", "-w", "/workspace",
               "-e", "HOME=/workspace", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        if self.policy.max_memory_mb:
            cmd += ["--memory", f"{self.policy.max_memory_mb}m"]
        if self.policy.max_cpus:
            cmd += ["--cpus", str(self.policy.max_cpus)]
        for key in self.policy.env_allowlist:
            if key in os.environ:
                cmd += ["-e", key]
        return [*cmd, self.image, *argv]

    async def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult:
        cmd = self.container_argv(list(argv))
        timeout = min(timeout_s or self.policy.max_wall_s, self.policy.max_wall_s)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        out, err, truncated, timed_out = await _collect(proc, timeout,
                                                        self.policy.max_output_bytes)
        return RunResult(argv=list(argv), exit_code=None if timed_out else proc.returncode,
                         stdout=out.decode("utf-8", "replace"),
                         stderr=err.decode("utf-8", "replace"),
                         duration_s=round(time.monotonic() - started, 3), timed_out=timed_out,
                         truncated=truncated, unenforced=[])

    def close(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


def default_sandbox(policy: SandboxPolicy, root: str | Path | None = None) -> Sandbox:
    """The strongest available sandbox that can honour ``policy``: a container when a
    runtime is installed, otherwise a local process sandbox (if the policy allows it)."""
    if ContainerSandbox.detect_runtime():
        return ContainerSandbox(policy, root=root)
    return LocalProcessSandbox(policy, root=root)
