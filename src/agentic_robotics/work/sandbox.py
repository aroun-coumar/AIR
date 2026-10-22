"""Sandboxes where agent work executes: research scripts, development builds and tests,
simulations, analyses.

A sandbox declares what it actually enforces (:class:`SandboxCapabilities`). A task's
:class:`SandboxPolicy` can require particular controls to be enforced. In ``strict`` mode
(the default) a sandbox that cannot enforce them refuses the work instead of running with
weaker isolation than claimed; in ``best_effort`` mode it runs and every result lists the
required controls that were *not* applied. Unenforced controls are recorded with every
run, so the evidence states the real isolation.

Adapters (each enforces exactly what its capabilities say, nothing is implied):

* :class:`LocalProcessSandbox`: always available. Private temporary workspace, no shell,
  command allowlist, scrubbed environment, wall-clock timeout with kill, output caps,
  isolated-mode Python. Memory and CPU limits through rlimits on POSIX and a Job Object
  on Windows. It does **not** jail the filesystem or the network.
* :class:`BubblewrapSandbox` (Linux, ``bwrap``): read-only root, writable workspace only,
  protected paths (``.git``) re-bound read-only, every namespace unshared (network
  included), ``--die-with-parent``, ``--clearenv``. With ``network: proxy`` the only way
  out is a Unix-socket bridge to the host-side egress proxy. No seccomp filter is loaded.
* :class:`LandlockSandbox` (Linux, ``landrun``): Landlock filesystem rules; network rules
  by *port* only (TCP from ABI 4, UDP from ABI 10). The Landlock ABI is probed and recorded.
* :class:`SeatbeltSandbox` (macOS, ``sandbox-exec``): a generated Seatbelt profile, deny by
  default, writes only to the workspace, protected paths denied, network none / any / only
  the local proxy port.
* :class:`ContainerSandbox`: Docker or Podman. ``--network none``, memory/CPU/PID limits,
  read-only root, dropped capabilities, workspace bind mount.
* Remote and microVM sandboxes: :mod:`agentic_robotics.work.remote`.

Every external tool is reached through an injectable :class:`ProcessRunner`, so command
construction is unit-tested without the tool installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePath
from typing import Any, Protocol

from ..core.errors import FrameworkError
from ..core.schema import Contract, contract
from .egress import EgressPolicy, EgressProxy, Resolver, system_resolver

__requirements__ = ("AIR01", "REQ02", "REQ04", "G15")

CONTROLS = ("filesystem", "network", "memory", "cpu", "time", "env", "output",
            "protected_paths")


class SandboxError(FrameworkError):
    code = "sandbox_error"


class Isolation(StrEnum):
    PROCESS = "process"
    CONTAINER = "container"
    VM = "vm"
    REMOTE = "remote"


class Placement(StrEnum):
    LOCAL = "local"     # this host
    REMOTE = "remote"   # another machine the project controls
    CLOUD = "cloud"     # a provider's infrastructure


class EnforcementMode(StrEnum):
    STRICT = "strict"            # refuse when a required control cannot be enforced
    BEST_EFFORT = "best_effort"  # run, and record which required controls were not applied


@dataclass(frozen=True)
class SandboxCapabilities:
    isolation: Isolation
    enforced: frozenset[str]          # subset of CONTROLS actually enforced
    detail: str = ""
    placement: Placement = Placement.LOCAL
    applied: tuple[str, ...] = ()     # the concrete mechanisms, for evidence
    landlock_abi: int | None = None
    data_leaves_device: bool = False

    def unenforced(self) -> list[str]:
        return [c for c in CONTROLS if c not in self.enforced]

    def to_dict(self) -> dict[str, Any]:
        return {"isolation": self.isolation.value, "placement": self.placement.value,
                "enforced": sorted(self.enforced), "unenforced": self.unenforced(),
                "applied": list(self.applied), "landlock_abi": self.landlock_abi,
                "data_leaves_device": self.data_leaves_device, "detail": self.detail}


class NetworkMode(StrEnum):
    NONE = "none"
    ANY = "any"
    PROXY = "proxy"   # 1.1: only through the egress proxy, under SandboxPolicy.egress


@contract("sandbox_policy", version="1.1")
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
    # 1.1
    mode: EnforcementMode = EnforcementMode.STRICT
    egress: EgressPolicy = field(default_factory=EgressPolicy)
    protected_paths: tuple[str, ...] = (".git",)   # workspace-relative, kept read-only
    deny_read: tuple[str, ...] = ()                # absolute host paths hidden from the sandbox

    def __post_init__(self) -> None:
        unknown = set(self.require_enforced) - set(CONTROLS)
        if unknown:
            raise SandboxError("unknown sandbox controls", unknown=sorted(unknown))
        problems = self.egress.problems()
        if self.egress.secrets and self.network is not NetworkMode.PROXY:
            problems.append("secrets need network mode 'proxy'")
        for rel in self.protected_paths:
            if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
                problems.append(f"protected path must be workspace-relative: {rel!r}")
        for p in self.deny_read:
            if not Path(p).is_absolute() and not p.startswith("/"):
                problems.append(f"deny_read path must be absolute: {p!r}")
        if problems:
            raise SandboxError("invalid sandbox policy", problems=problems)


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
    not_applied: list[str] = field(default_factory=list)   # required but not applied
    applied: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)  # denied egress
    egress: list[dict[str, Any]] = field(default_factory=list)      # all egress decisions
    placement: Placement = Placement.LOCAL

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


# -- process runner ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProcessSpec:
    argv: list[str]
    cwd: str
    env: dict[str, str]
    timeout_s: float
    output_limit: int
    memory_mb: int | None = None
    cpus: float | None = None
    wall_s: float = 120.0


@dataclass
class ProcessOutcome:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    truncated: bool = False
    timed_out: bool = False
    applied: list[str] = field(default_factory=list)


class ProcessRunner(Protocol):
    def enforceable(self) -> frozenset[str]: ...

    async def __call__(self, spec: ProcessSpec) -> ProcessOutcome: ...


async def _collect(proc: asyncio.subprocess.Process, timeout_s: float, limit: int,
                   on_timeout: Callable[[], None] | None = None
                   ) -> tuple[bytes, bytes, bool, bool]:
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
        if on_timeout is not None:
            on_timeout()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return b"", b"", False, True


class SubprocessRunner:
    """Runs a process for real: rlimits on POSIX, a Job Object on Windows."""

    def enforceable(self) -> frozenset[str]:
        if os.name == "posix":
            return frozenset({"memory", "cpu"})
        if sys.platform == "win32":
            from . import winjob

            if winjob.available():
                return frozenset({"memory", "cpu"})
        return frozenset()

    async def __call__(self, spec: ProcessSpec) -> ProcessOutcome:
        kwargs: dict[str, Any] = {}
        applied: list[str] = []
        job: Any = None
        if os.name == "posix":
            kwargs["preexec_fn"] = _rlimits(spec.memory_mb, spec.cpus, spec.wall_s)
            if spec.memory_mb:
                applied.append(f"rlimit_as={spec.memory_mb}MiB")
            if spec.cpus:
                applied.append(f"rlimit_cpu={int(spec.wall_s * spec.cpus) + 1}s")
        elif sys.platform == "win32" and (spec.memory_mb or spec.cpus):
            from . import winjob

            try:
                job = winjob.Job(spec.memory_mb, spec.cpus, spec.wall_s)
            except OSError as exc:
                raise SandboxError("cannot create a Windows job object", error=str(exc)) from exc
            kwargs["creationflags"] = winjob.CREATE_SUSPENDED
            applied.extend(f"job:{a}" for a in job.applied)
        try:
            proc = await asyncio.create_subprocess_exec(
                *spec.argv, cwd=spec.cwd, env=spec.env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
            if job is not None:
                try:
                    job.adopt(proc.pid)
                except OSError as exc:
                    proc.kill()
                    await proc.wait()
                    raise SandboxError("cannot place the process in its job object",
                                       error=str(exc)) from exc
            out, err, truncated, timed_out = await _collect(
                proc, spec.timeout_s, spec.output_limit,
                on_timeout=job.terminate if job is not None else None)
            return ProcessOutcome(None if timed_out else proc.returncode, out, err, truncated,
                                  timed_out, applied)
        finally:
            if job is not None:
                job.terminate()
                job.close()


def _rlimits(memory_mb: int | None, cpus: float | None, wall_s: float) -> Callable[[], None]:
    if sys.platform == "win32":
        raise SandboxError("rlimits are POSIX-only")
    import resource

    def apply() -> None:  # runs in the child before exec
        if memory_mb:
            resource.setrlimit(resource.RLIMIT_AS, (memory_mb * 2**20, memory_mb * 2**20))
        if cpus:
            cpu_s = int(wall_s * cpus) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
    return apply


# -- workspace and common behaviour -------------------------------------------------------------
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
        self._check_protected(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
        return p

    def import_file(self, source: Path, rel: str) -> Path:
        p = self.path(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, p)
        return p

    def _check_protected(self, target: Path) -> None:
        policy: SandboxPolicy | None = getattr(self, "policy", None)
        if policy is None:
            return
        for rel in policy.protected_paths:
            guarded = (self.workspace / rel).resolve()
            if target == guarded or target.is_relative_to(guarded):
                raise SandboxError("path is protected (read-only) by the sandbox policy",
                                   path=str(target.relative_to(self.workspace.resolve())))

    def list_files(self, rel: str = ".") -> list[str]:
        base = self.path(rel) if rel not in (".", "") else self.workspace.resolve()
        if not base.exists():
            return []
        out = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and ".air-tmp" not in p.parts:
                out.append(p.resolve().relative_to(self.workspace.resolve()).as_posix())
        return out


class _ProcessSandbox(_WorkspaceMixin):
    """Common lifecycle: capability check (strict / best effort), workspace, proxy,
    environment, running through the :class:`ProcessRunner`, evidence on the result."""

    name = "process"

    def __init__(self, policy: SandboxPolicy | None = None, *,
                 root: str | Path | None = None, runner: ProcessRunner | None = None,
                 resolver: Resolver = system_resolver,
                 environ: dict[str, str] | None = None) -> None:
        self.policy = policy or SandboxPolicy()
        self.runner: ProcessRunner = runner or SubprocessRunner()
        self.resolver = resolver
        self.environ = dict(os.environ) if environ is None else environ
        self.capabilities = self._capabilities()
        problems = policy_problems(self.policy, self.capabilities)
        if problems and self.policy.mode is EnforcementMode.STRICT:
            raise SandboxError("sandbox cannot honour the policy", problems=problems,
                               sandbox=self.name)
        self.not_applied = [c for c in self.policy.require_enforced
                            if c not in self.capabilities.enforced]
        self.workspace = Path(tempfile.mkdtemp(prefix="air-sbx-", dir=root)).resolve()
        (self.workspace / ".air-tmp").mkdir()
        self.proxy: EgressProxy | None = None
        self._proxy_env: dict[str, str] = {}
        self._aux: list[Path] = []

    # subclass hooks
    def _capabilities(self) -> SandboxCapabilities:
        raise NotImplementedError

    def _wrap(self, cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        return cmd, env

    def _start_proxy(self) -> dict[str, str]:
        assert self.proxy is not None
        self.proxy.start_tcp()
        return self.proxy.sandbox_env()

    # common
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

    def _env(self) -> dict[str, str]:
        tmp = str(self.workspace / ".air-tmp")
        env = {"PATH": os.pathsep.join([str(Path(sys.executable).parent)]),
               "TMP": tmp, "TEMP": tmp, "TMPDIR": tmp, "HOME": str(self.workspace),
               "USERPROFILE": str(self.workspace), "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}
        if os.name == "nt":
            for key in ("SYSTEMROOT", "WINDIR", "COMSPEC"):
                if key in self.environ:
                    env[key] = self.environ[key]
        for key in self.policy.env_allowlist:
            if key in self.environ:
                env[key] = self.environ[key]
        if self.policy.network is NetworkMode.PROXY:
            if self.proxy is None:
                self.proxy = EgressProxy(self.policy.egress, self.resolver, self.environ)
                self._proxy_env = self._start_proxy()
            env.update(self._proxy_env)
        return env

    async def run(self, argv: list[str], timeout_s: float | None = None) -> RunResult:
        cmd = self._resolve(list(argv))
        timeout = min(timeout_s or self.policy.max_wall_s, self.policy.max_wall_s)
        env = self._env()
        wrapped, outer_env = self._wrap(cmd, env)
        seq = self.proxy.last_seq if self.proxy else 0
        started = time.monotonic()
        outcome = await self.runner(ProcessSpec(
            wrapped, str(self.workspace), outer_env, timeout, self.policy.max_output_bytes,
            self.policy.max_memory_mb if "memory" in self.capabilities.enforced else None,
            self.policy.max_cpus if "cpu" in self.capabilities.enforced else None,
            self.policy.max_wall_s))
        events = [e.to_dict() for e in self.proxy.events_since(seq)] if self.proxy else []
        return RunResult(argv=list(argv), exit_code=outcome.exit_code,
                         stdout=outcome.stdout.decode("utf-8", "replace"),
                         stderr=outcome.stderr.decode("utf-8", "replace"),
                         duration_s=round(time.monotonic() - started, 3),
                         timed_out=outcome.timed_out, truncated=outcome.truncated,
                         unenforced=self.capabilities.unenforced(),
                         not_applied=list(self.not_applied),
                         applied=[*self.capabilities.applied, *outcome.applied],
                         violations=[e for e in events if not e["allowed"]], egress=events,
                         placement=self.capabilities.placement)

    def close(self) -> None:
        if self.proxy is not None:
            self.proxy.close()
        shutil.rmtree(self.workspace, ignore_errors=True)
        for p in self._aux:
            shutil.rmtree(p, ignore_errors=True)


class LocalProcessSandbox(_ProcessSandbox):
    name = "local"

    def _capabilities(self) -> SandboxCapabilities:
        enforced = {"time", "env", "output"} | set(self.runner.enforceable())
        mech = ["private workspace", "no shell", "command allowlist", "scrubbed env",
                "timeout+kill", "output cap"]
        if sys.platform == "win32" and "memory" in enforced:
            mech.append("windows job object")
        elif os.name == "posix":
            mech.append("rlimits")
        return SandboxCapabilities(
            Isolation.PROCESS, frozenset(enforced),
            "private workspace, no shell, command allowlist, scrubbed env, timeouts; "
            "no filesystem jail or network isolation (a proxied process can ignore the "
            "proxy)", applied=tuple(mech))


# -- Linux: bubblewrap --------------------------------------------------------------------------
BRIDGE_PORT = 3128


def _read_paths() -> list[str]:
    """Interpreter and import locations the sandboxed Python needs to read."""
    out: list[str] = []
    for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, *sys.path):
        if p and os.path.isabs(p) and os.path.exists(p) and p not in out:
            out.append(p)
    return out


def bwrap_argv(bwrap: str, policy: SandboxPolicy, workspace: Path, cmd: Sequence[str],
               env: dict[str, str], *, proxy_dir: Path | None = None,
               proxy_socket: str | None = None, python: str = sys.executable,
               bridge: str | None = None) -> list[str]:
    """The ``bwrap`` command line for ``cmd`` under ``policy`` (pure; unit-tested)."""
    ws = str(workspace)
    argv = [bwrap, "--die-with-parent", "--new-session", "--unshare-all"]
    if policy.network is NetworkMode.ANY:
        argv.append("--share-net")
    argv += ["--clearenv"]
    for k, v in sorted(env.items()):
        argv += ["--setenv", k, v]
    argv += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for p in policy.deny_read:
        if os.path.isdir(p):
            argv += ["--tmpfs", p]
        else:
            argv += ["--ro-bind-try", "/dev/null", p]
    argv += ["--bind", ws, ws]
    for rel in policy.protected_paths:
        target = str(workspace / rel)
        if os.path.exists(target):
            argv += ["--ro-bind", target, target]
        else:  # block creation: mount /dev/null on the missing path (codex linux-sandbox)
            argv += ["--ro-bind", "/dev/null", target]
    if policy.network is NetworkMode.PROXY:
        if proxy_dir is None or proxy_socket is None:
            raise SandboxError("proxy mode needs the proxy socket")
        argv += ["--ro-bind", str(proxy_dir), str(proxy_dir)]
    argv += ["--chdir", ws, "--"]
    if policy.network is NetworkMode.PROXY:
        bridge = bridge or str(Path(__file__).with_name("netbridge.py"))
        argv += [python, "-I", "-B", bridge, str(proxy_socket), str(BRIDGE_PORT), "--"]
    return [*argv, *cmd]


class BubblewrapSandbox(_ProcessSandbox):
    name = "bubblewrap"

    def __init__(self, policy: SandboxPolicy | None = None, *, bwrap: str | None = None,
                 **kw: Any) -> None:
        self.bwrap = bwrap or self.detect()
        if self.bwrap is None:
            raise SandboxError("bubblewrap (bwrap) is not installed")
        super().__init__(policy, **kw)

    @staticmethod
    def detect() -> str | None:
        return shutil.which("bwrap") if sys.platform.startswith("linux") else None

    def _capabilities(self) -> SandboxCapabilities:
        enforced = {"filesystem", "network", "time", "env", "output", "protected_paths"} | \
            set(self.runner.enforceable())
        applied = ["bwrap --ro-bind / /", "--bind workspace", "--unshare-all",
                   "--die-with-parent", "--new-session", "--clearenv",
                   "protected paths --ro-bind"]
        applied.append({NetworkMode.NONE: "network namespace empty",
                        NetworkMode.ANY: "--share-net (host network)",
                        NetworkMode.PROXY: "network only via egress proxy bridge"}[
                            self.policy.network])
        return SandboxCapabilities(
            Isolation.PROCESS, frozenset(enforced),
            "bubblewrap namespaces; no seccomp filter is loaded (not applied)",
            applied=tuple(applied))

    def _start_proxy(self) -> dict[str, str]:
        assert self.proxy is not None
        d = Path(tempfile.mkdtemp(prefix="air-proxy-"))
        self._aux.append(d)
        self._proxy_dir = d
        self._proxy_sock = str(d / "proxy.sock")
        self.proxy.start_unix(self._proxy_sock)
        return self.proxy.sandbox_env(f"http://127.0.0.1:{BRIDGE_PORT}")

    def _wrap(self, cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        sandbox_env = {**env, "TMPDIR": str(self.workspace / ".air-tmp")}
        argv = bwrap_argv(str(self.bwrap), self.policy, self.workspace, cmd, sandbox_env,
                          proxy_dir=getattr(self, "_proxy_dir", None),
                          proxy_socket=getattr(self, "_proxy_sock", None))
        return argv, {"PATH": "/usr/bin:/bin"}


# -- Linux: Landlock via landrun ----------------------------------------------------------------
def landlock_abi() -> int | None:
    """The kernel's Landlock ABI version (``landlock_create_ruleset`` with the VERSION
    flag), or ``None`` when Landlock is unavailable or this is not Linux x86-64/arm64."""
    if not sys.platform.startswith("linux"):
        return None
    import ctypes
    import platform

    if platform.machine() not in ("x86_64", "AMD64", "aarch64", "arm64"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        abi = int(libc.syscall(444, None, ctypes.c_size_t(0), ctypes.c_uint32(1)))
    except (OSError, AttributeError):
        return None
    return abi if abi > 0 else None


def landrun_argv(landrun: str, policy: SandboxPolicy, workspace: Path, cmd: Sequence[str],
                 env: dict[str, str], *, abi: int | None, proxy_port: int | None = None,
                 read_paths: Sequence[str] = ()) -> list[str]:
    """The ``landrun`` command line (pure; unit-tested)."""
    argv = [landrun]
    if abi is None or abi < 10:
        argv.append("--best-effort")  # AIR checked the ABI itself and refused in strict mode
    argv += ["--ignore-missing", "--rox", "/usr,/bin,/sbin,/lib,/lib64",
             "--ro", "/etc,/dev,/proc", "--rw", "/dev/null"]
    for p in read_paths:
        argv += ["--rox", p]
    argv += ["--rw", str(workspace)]
    for k, v in sorted(env.items()):
        argv += ["--env", f"{k}={v}"]
    if policy.network is NetworkMode.ANY:
        argv.append("--unrestricted-network")
    elif policy.network is NetworkMode.PROXY:
        if proxy_port is None:
            raise SandboxError("proxy mode needs the proxy port")
        argv += ["--connect-tcp", str(proxy_port)]
    return [*argv, "--", *cmd]


class LandlockSandbox(_ProcessSandbox):
    name = "landlock"

    def __init__(self, policy: SandboxPolicy | None = None, *, landrun: str | None = None,
                 abi: int | Callable[[], int | None] | None = landlock_abi, **kw: Any) -> None:
        self.landrun = landrun or shutil.which("landrun")
        if self.landrun is None:
            raise SandboxError("landrun is not installed")
        self.abi = abi() if callable(abi) else abi
        super().__init__(policy, **kw)

    def _capabilities(self) -> SandboxCapabilities:
        abi = self.abi
        enforced = {"time", "env", "output"} | set(self.runner.enforceable())
        applied = []
        if abi:
            enforced.add("filesystem")
            applied.append(f"landlock ABI v{abi} filesystem rules")
        net = self.policy.network
        if net is NetworkMode.ANY:
            enforced.add("network")
        elif net is NetworkMode.NONE and abi is not None and abi >= 10:
            enforced.add("network")
            applied.append("landlock TCP and UDP denied")
        elif net is NetworkMode.NONE and abi is not None and abi >= 4:
            applied.append("landlock TCP denied (UDP needs ABI v10: not applied)")
        elif net is NetworkMode.PROXY:
            applied.append("landlock: proxy port only, by port not host (not a host filter)")
        return SandboxCapabilities(
            Isolation.PROCESS, frozenset(enforced),
            "Landlock via landrun; network rules are by port only; protected paths inside "
            "the writable workspace cannot be re-protected (not applied); files opened "
            "before sandboxing and Multipath TCP are outside Landlock", applied=tuple(applied),
            landlock_abi=abi)

    def _wrap(self, cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        port = None
        if self.proxy is not None:
            port = int(self.proxy.address.rsplit(":", 1)[1])
        argv = landrun_argv(str(self.landrun), self.policy, self.workspace, cmd, env,
                            abi=self.abi, proxy_port=port, read_paths=_read_paths())
        return argv, {"PATH": "/usr/bin:/bin"}


# -- macOS: Seatbelt ------------------------------------------------------------------------------
def _sb(path: str | PurePath) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(policy: SandboxPolicy, workspace: PurePath, *,
                     proxy_port: int | None = None) -> str:
    """A Seatbelt (SBPL) profile for ``policy``: deny by default; read everywhere except
    ``deny_read``; write only the workspace (and ``/dev/null``); protected paths denied;
    network none, any, or only ``localhost:<proxy_port>``. Later rules take precedence.
    Shape informed by openai/codex ``seatbelt_base_policy.sbpl`` (Apache-2.0)."""
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target same-sandbox))",
        "(allow process-info* (target same-sandbox))",
        "(allow sysctl-read)",
        "(allow mach-lookup (global-name \"com.apple.system.opendirectoryd.libinfo\"))",
        "(allow ipc-posix-sem)",
        "(allow file-read*)",
    ]
    for p in policy.deny_read:
        lines.append(f"(deny file-read* (subpath {_sb(p)}))")
    lines.append(f"(allow file-write* (subpath {_sb(workspace)}) (literal \"/dev/null\"))")
    for rel in policy.protected_paths:
        lines.append(f"(deny file-write* (subpath {_sb(workspace / rel)}))")
    if policy.network is NetworkMode.ANY:
        lines.append("(allow network*)")
    elif policy.network is NetworkMode.PROXY:
        if proxy_port is None:
            raise SandboxError("proxy mode needs the proxy port")
        lines.append(f"(allow network-outbound (remote ip \"localhost:{proxy_port}\"))")
    return "\n".join(lines) + "\n"


class SeatbeltSandbox(_ProcessSandbox):
    name = "seatbelt"

    def __init__(self, policy: SandboxPolicy | None = None, *, sandbox_exec: str | None = None,
                 **kw: Any) -> None:
        self.sandbox_exec = sandbox_exec or self.detect()
        if self.sandbox_exec is None:
            raise SandboxError("sandbox-exec (macOS Seatbelt) is not available")
        super().__init__(policy, **kw)

    @staticmethod
    def detect() -> str | None:
        return shutil.which("sandbox-exec") if sys.platform == "darwin" else None

    def _capabilities(self) -> SandboxCapabilities:
        enforced = {"filesystem", "network", "time", "env", "output", "protected_paths"}
        enforced |= set(self.runner.enforceable()) - {"memory"}  # macOS ignores RLIMIT_AS
        return SandboxCapabilities(
            Isolation.PROCESS, frozenset(enforced),
            "Seatbelt profile via sandbox-exec (deprecated by Apple but shipped); memory "
            "limits are not enforced on macOS",
            applied=("seatbelt deny-default profile", "writes: workspace only",
                     f"network: {self.policy.network.value}"))

    def _wrap(self, cmd: list[str], env: dict[str, str]) -> tuple[list[str], dict[str, str]]:
        port = int(self.proxy.address.rsplit(":", 1)[1]) if self.proxy is not None else None
        profile = seatbelt_profile(self.policy, self.workspace, proxy_port=port)
        return [str(self.sandbox_exec), "-p", profile, *cmd], env


# -- containers ---------------------------------------------------------------------------------
class ContainerSandbox(_WorkspaceMixin):
    """Docker/Podman sandbox. Network isolation, resource limits and a read-only root
    are enforced by the container runtime."""

    name = "container"

    def __init__(self, policy: SandboxPolicy | None = None, *, image: str = "python:3.12-slim",
                 runtime: str | None = None, root: str | Path | None = None,
                 runner: ProcessRunner | None = None) -> None:
        self.policy = policy or SandboxPolicy()
        self.image = image
        self.runtime = runtime or self.detect_runtime()
        if self.runtime is None:
            raise SandboxError("no container runtime (docker or podman) is installed")
        self.runner: ProcessRunner = runner or SubprocessRunner()
        enforced = set(CONTROLS)
        if self.policy.network is NetworkMode.PROXY:
            enforced.discard("network")  # the host proxy is not reachable from --network none
        self.capabilities = SandboxCapabilities(
            Isolation.CONTAINER, frozenset(enforced), f"{Path(self.runtime).name}: {image}",
            applied=("--network none" if self.policy.network is not NetworkMode.ANY
                     else "--network bridge", "--read-only", "--cap-drop ALL",
                     "no-new-privileges", "pids/memory/cpu limits"))
        problems = policy_problems(self.policy, self.capabilities)
        if problems and self.policy.mode is EnforcementMode.STRICT:
            raise SandboxError("sandbox cannot honour the policy", problems=problems,
                               sandbox=self.name)
        self.not_applied = [c for c in self.policy.require_enforced
                            if c not in self.capabilities.enforced]
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
               "--network", "bridge" if self.policy.network is NetworkMode.ANY else "none",
               "--read-only", "--tmpfs", "/tmp:rw,size=256m", "--pids-limit", "256",
               "--security-opt", "no-new-privileges", "--cap-drop", "ALL",
               "-v", f"{self.workspace}:/workspace:rw", "-w", "/workspace",
               "-e", "HOME=/workspace", "-e", "PYTHONDONTWRITEBYTECODE=1"]
        for rel in self.policy.protected_paths:
            if (self.workspace / rel).exists():
                cmd += ["-v", f"{self.workspace / rel}:/workspace/{rel}:ro"]
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
        outcome = await self.runner(ProcessSpec(cmd, str(self.workspace),
                                                {"PATH": os.environ.get("PATH", "")}, timeout,
                                                self.policy.max_output_bytes,
                                                wall_s=self.policy.max_wall_s))
        return RunResult(argv=list(argv), exit_code=outcome.exit_code,
                         stdout=outcome.stdout.decode("utf-8", "replace"),
                         stderr=outcome.stderr.decode("utf-8", "replace"),
                         duration_s=round(time.monotonic() - started, 3),
                         timed_out=outcome.timed_out, truncated=outcome.truncated,
                         unenforced=self.capabilities.unenforced(),
                         not_applied=list(self.not_applied),
                         applied=list(self.capabilities.applied))

    def close(self) -> None:
        shutil.rmtree(self.workspace, ignore_errors=True)


# -- selection and probing --------------------------------------------------------------------
ADAPTERS: tuple[tuple[str, Callable[..., Sandbox]], ...] = (
    ("container", ContainerSandbox), ("bubblewrap", BubblewrapSandbox),
    ("seatbelt", SeatbeltSandbox), ("landlock", LandlockSandbox),
    ("local", LocalProcessSandbox))


@dataclass(frozen=True)
class AdapterProbe:
    name: str
    available: bool
    honours_policy: bool
    capabilities: SandboxCapabilities | None
    problems: tuple[str, ...]


def probe_adapters(policy: SandboxPolicy | None = None) -> list[AdapterProbe]:
    """Which adapters exist on this host, what each enforces, and whether it can honour
    ``policy`` (best-effort probe: a best_effort copy of the policy is used to describe)."""
    policy = policy or SandboxPolicy()
    describe = policy.evolve(mode=EnforcementMode.BEST_EFFORT)
    out = []
    for name, factory in ADAPTERS:
        try:
            sb = factory(describe)
        except SandboxError as exc:
            out.append(AdapterProbe(name, False, False, None, (exc.message,)))
            continue
        try:
            problems = tuple(policy_problems(policy, sb.capabilities))
            out.append(AdapterProbe(name, True, not problems, sb.capabilities, problems))
        finally:
            sb.close()
    return out


def default_sandbox(policy: SandboxPolicy, root: str | Path | None = None) -> Sandbox:
    """The strongest available sandbox that can honour ``policy``: a container runtime,
    then bubblewrap, Seatbelt, Landlock, and finally a local process sandbox (which
    refuses if the policy needs controls it cannot enforce)."""
    for _name, factory in ADAPTERS[:-1]:
        try:
            return factory(policy, root=root)
        except SandboxError:
            continue
    return LocalProcessSandbox(policy, root=root)


__all__ = ["ADAPTERS", "CONTROLS", "AdapterProbe", "BubblewrapSandbox", "ContainerSandbox",
           "EnforcementMode", "Isolation", "LandlockSandbox", "LocalProcessSandbox",
           "NetworkMode", "Placement", "ProcessOutcome", "ProcessRunner", "ProcessSpec",
           "RunResult", "Sandbox", "SandboxCapabilities", "SandboxError", "SandboxPolicy",
           "SeatbeltSandbox", "SubprocessRunner", "bwrap_argv", "default_sandbox",
           "landlock_abi", "landrun_argv", "policy_problems", "probe_adapters",
           "seatbelt_profile"]
