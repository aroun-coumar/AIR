"""Out-of-process backends over a JSON-lines protocol (``air.backend/1``).

Heavy simulators (MuJoCo, Isaac, Gazebo, FMUs) run in their own process, with their own
dependencies and licenses. The parent speaks one JSON object per line:

    -> {"id": 1, "op": "hello"}
    <- {"id": 1, "ok": true, "result": {"protocol": "air.backend/1", "name": ..., ...}}
    -> {"id": 2, "op": "load", "args": {"scenario": {...}, "time_policy": {...}, ...}}
    <- {"id": 2, "ok": false, "error": {"code": "unsupported_feature", ...}}

Ops: ``hello load reset step snapshot restore rng_state set_rng_state metrics close``.
Errors keep their code, so an unsupported feature still raises
:class:`UnsupportedFeature` in the parent. Serve any backend with :func:`serve`, or run
``python -m agentic_robotics.engineering.serve <backend>``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import IO, Any, Protocol

from ..core.errors import FrameworkError, SchemaError
from ..core.schema import _dump, _load
from .backends import (
    BackendCapabilities,
    BackendInfo,
    ExperimentBackend,
    RestoreLevel,
    Snapshot,
    TimePolicy,
    UnsupportedFeature,
)

__requirements__ = ("REQ04", "G03")

PROTOCOL = "air.backend/1"


class BackendProcessError(FrameworkError):
    code = "backend_process_error"


class Transport(Protocol):
    """Send one request object, receive one response object."""

    def request(self, message: dict[str, Any]) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SubprocessTransport:
    """Runs ``argv`` with pipes; one JSON line per message."""

    def __init__(self, argv: Sequence[str], timeout_s: float = 30.0) -> None:
        self.argv = list(argv)
        self.timeout_s = timeout_s
        self._proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                      bufsize=1)

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        proc = self._proc
        if proc.stdin is None or proc.stdout is None or proc.poll() is not None:
            raise BackendProcessError("backend process is not running", argv=self.argv)
        proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        if not line:
            raise BackendProcessError("backend process closed its output", argv=self.argv,
                                      returncode=proc.poll())
        try:
            reply = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BackendProcessError("backend sent invalid JSON", line=line[:200]) from exc
        if not isinstance(reply, dict):
            raise BackendProcessError("backend reply is not an object")
        return reply

    def close(self) -> None:
        proc = self._proc
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        try:
            proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()


_ERRORS: dict[str, type[FrameworkError]] = {
    "unsupported_feature": UnsupportedFeature, "schema_error": SchemaError,
}


class ProcessBackend:
    """Client side: an :class:`ExperimentBackend` whose implementation is in another
    process. Its capabilities are those the server *declares* in ``hello``."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport
        self._seq = 0
        hello = self._call("hello")
        if hello.get("protocol") != PROTOCOL:
            raise BackendProcessError("backend speaks a different protocol",
                                      expected=PROTOCOL, actual=hello.get("protocol"))
        self.name = str(hello["name"])
        self.version = str(hello["version"])
        self.capabilities: BackendCapabilities = _load(
            BackendCapabilities, hello["capabilities"], "capabilities")

    def _call(self, op: str, **args: Any) -> Any:
        self._seq += 1
        reply = self._transport.request({"id": self._seq, "op": op, "args": args})
        if reply.get("id") != self._seq:
            raise BackendProcessError("reply id does not match request", op=op,
                                      expected=self._seq, actual=reply.get("id"))
        if not reply.get("ok"):
            err = reply.get("error") or {}
            cls = _ERRORS.get(str(err.get("code")), BackendProcessError)
            raise cls(str(err.get("message", "backend error")), op=op,
                      **dict(err.get("details") or {}))
        return reply.get("result")

    def load(self, scenario: Mapping[str, Any], time_policy: TimePolicy, *,
             features: Iterable[str] = (), device: str = "cpu") -> BackendInfo:
        info = BackendInfo.from_dict(self._call(
            "load", scenario=dict(scenario), time_policy=_dump(time_policy),
            features=list(features), device=device))
        return info.evolve(out_of_process=True)

    def reset(self, seed: int | None) -> dict[str, Any]:
        return dict(self._call("reset", seed=seed))

    def step(self, action: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._call("step", action=dict(action)))

    def snapshot(self) -> Snapshot:
        r = self._call("snapshot")
        return Snapshot(state=r["state"], level=RestoreLevel(r["level"]),
                        lossy=tuple(r.get("lossy", ())))

    def restore(self, snapshot: Snapshot) -> None:
        self._call("restore", state=snapshot.state, level=snapshot.level.value,
                   lossy=list(snapshot.lossy))

    def rng_state(self) -> Any:
        return self._call("rng_state")

    def set_rng_state(self, state: Any) -> None:
        self._call("set_rng_state", state=state)

    def metrics(self) -> dict[str, float]:
        return {k: float(v) for k, v in self._call("metrics").items()}

    def close(self) -> None:
        try:
            self._call("close")
        finally:
            self._transport.close()


def handle(backend: ExperimentBackend, message: Mapping[str, Any]) -> dict[str, Any]:
    """Server side: apply one request to ``backend`` and build the reply."""
    mid = message.get("id")
    op = message.get("op")
    args = dict(message.get("args") or {})
    try:
        result: Any
        if op == "hello":
            result = {"protocol": PROTOCOL, "name": backend.name, "version": backend.version,
                      "capabilities": _dump(backend.capabilities)}
        elif op == "load":
            tp = _load(TimePolicy, args["time_policy"], "time_policy")
            result = backend.load(args.get("scenario", {}), tp,
                                  features=args.get("features", ()),
                                  device=str(args.get("device", "cpu"))).to_dict()
        elif op == "reset":
            result = backend.reset(args.get("seed"))
        elif op == "step":
            result = backend.step(args.get("action", {}))
        elif op == "snapshot":
            snap = backend.snapshot()
            result = {"state": snap.state, "level": snap.level.value,
                      "lossy": list(snap.lossy)}
        elif op == "restore":
            backend.restore(Snapshot(state=args["state"], level=RestoreLevel(args["level"]),
                                     lossy=tuple(args.get("lossy", ()))))
            result = None
        elif op == "rng_state":
            result = backend.rng_state()
        elif op == "set_rng_state":
            backend.set_rng_state(args.get("state"))
            result = None
        elif op == "metrics":
            result = backend.metrics()
        elif op == "close":
            backend.close()
            result = None
        else:
            raise SchemaError("unknown op", op=op)
        return {"id": mid, "ok": True, "result": result}
    except FrameworkError as exc:
        return {"id": mid, "ok": False, "error": {
            "code": exc.code, "message": exc.message,
            "details": json.loads(json.dumps(exc.details, default=str))}}
    except (KeyError, TypeError, ValueError) as exc:
        return {"id": mid, "ok": False, "error": {
            "code": "backend_process_error", "message": f"{type(exc).__name__}: {exc}",
            "details": {}}}


def serve(backend: ExperimentBackend, rfile: IO[str], wfile: IO[str]) -> None:
    """Serve ``backend`` until ``close`` or end of input."""
    for line in rfile:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            reply: dict[str, Any] = {"id": None, "ok": False, "error": {
                "code": "backend_process_error", "message": "invalid JSON", "details": {}}}
        else:
            reply = handle(backend, message)
        wfile.write(json.dumps(reply) + "\n")
        wfile.flush()
        if isinstance(message, dict) and message.get("op") == "close":
            return


def process_factory(backend_name: str, python: str | None = None
                    ) -> Callable[[], ExperimentBackend]:
    """A factory that serves the registered backend ``backend_name`` in a child process."""

    def make() -> ExperimentBackend:
        argv = [python or sys.executable, "-m", "agentic_robotics.engineering.serve",
                backend_name]
        return ProcessBackend(SubprocessTransport(argv))

    return make
