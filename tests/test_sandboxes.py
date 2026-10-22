"""Enforcing sandbox adapters, strict vs best effort, egress proxy, remote sandboxes
(catalog W4.1-W4.4). External tools run through a fake runner; real runs skip when the
tool is absent."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import shutil
import socket
import sys
import threading
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

from agentic_robotics.agents.providers import Location, ModelRouter, ScriptedChat, call, say
from agentic_robotics.provenance import Project
from agentic_robotics.provenance.traceability import baseline_requirements
from agentic_robotics.work import (
    BubblewrapSandbox,
    DataClass,
    EgressPolicy,
    EgressProxy,
    EnforcementMode,
    FakeRemoteClient,
    LandlockSandbox,
    LocalProcessSandbox,
    NetworkMode,
    Placement,
    RemoteSandbox,
    SandboxError,
    SandboxPolicy,
    SeatbeltSandbox,
    SecretBinding,
    WorkAgent,
    WorkKind,
    WorkStatus,
    WorkTask,
    denied_range,
    probe_adapters,
)
from agentic_robotics.work.egress import EgressDecider, EgressError
from agentic_robotics.work.sandbox import (
    ProcessOutcome,
    ProcessSpec,
    bwrap_argv,
    landrun_argv,
    seatbelt_profile,
)

pytestmark = pytest.mark.req("G15", "REQ02", "REQ04", "AIR01")


class FakeRunner:
    """Records what would be executed; never runs anything."""

    def __init__(self, enforce: frozenset[str] = frozenset({"memory", "cpu"})) -> None:
        self.specs: list[ProcessSpec] = []
        self.enforce = enforce

    def enforceable(self) -> frozenset[str]:
        return self.enforce

    async def __call__(self, spec: ProcessSpec) -> ProcessOutcome:
        self.specs.append(spec)
        return ProcessOutcome(0, b"ok\n", b"", applied=["fake"])


def fake_resolver(table: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError("nxdomain")
        return table[host]
    return resolve


# -- W4.1 bubblewrap ------------------------------------------------------------------------------
def test_bwrap_command_line_is_read_only_root_writable_workspace_and_no_network(
        tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / ".git").mkdir(parents=True)
    policy = SandboxPolicy(deny_read=(str(tmp_path / "secrets"),))
    (tmp_path / "secrets").mkdir()
    argv = bwrap_argv("bwrap", policy, ws, ["/usr/bin/python3", "job.py"], {"HOME": str(ws)})
    joined = " ".join(argv)
    for flag in ("--die-with-parent", "--new-session", "--unshare-all", "--clearenv",
                 "--ro-bind / /", f"--bind {ws} {ws}", f"--ro-bind {ws / '.git'} {ws / '.git'}",
                 f"--tmpfs {tmp_path / 'secrets'}", f"--setenv HOME {ws}", f"--chdir {ws}"):
        assert flag in joined, flag
    assert "--share-net" not in argv
    # the workspace bind comes after the read-only root; .git is re-bound after the workspace
    assert joined.index("--ro-bind / /") < joined.index(f"--bind {ws}") < joined.index(
        f"--ro-bind {ws / '.git'}")
    assert argv[-2:] == ["/usr/bin/python3", "job.py"] and argv[-3] == "--"


def test_bwrap_missing_protected_path_is_blocked_and_any_network_is_shared(
        tmp_path: Path) -> None:
    argv = bwrap_argv("bwrap", SandboxPolicy(network=NetworkMode.ANY), tmp_path, ["python"], {})
    assert f"--ro-bind /dev/null {tmp_path / '.git'}" in " ".join(argv)
    assert "--share-net" in argv


def test_bwrap_proxy_mode_bridges_only_to_the_proxy_socket(tmp_path: Path) -> None:
    policy = SandboxPolicy(network=NetworkMode.PROXY,
                           egress=EgressPolicy(allow_domains=("pypi.org",)))
    argv = bwrap_argv("bwrap", policy, tmp_path, ["python", "x.py"], {},
                      proxy_dir=Path("/run/p"), proxy_socket="/run/p/s.sock", python="/py")
    j = " ".join(argv)
    pdir = str(Path("/run/p"))
    assert "--share-net" not in argv and f"--ro-bind {pdir} {pdir}" in j
    tail = argv[argv.index("--", argv.index("--chdir")) + 1:]
    assert tail[:3] == ["/py", "-I", "-B"] and tail[3].endswith("netbridge.py")
    assert tail[4:7] == ["/run/p/s.sock", "3128", "--"] and tail[-2:] == ["python", "x.py"]
    with pytest.raises(SandboxError):
        bwrap_argv("bwrap", policy, tmp_path, ["python"], {})


async def test_bubblewrap_adapter_runs_through_its_runner(tmp_path: Path) -> None:
    runner = FakeRunner()
    sb = BubblewrapSandbox(SandboxPolicy(require_enforced=("filesystem", "network", "env",
                                                           "protected_paths")),
                           bwrap="/usr/bin/bwrap", runner=runner, root=tmp_path)
    try:
        assert {"filesystem", "network", "protected_paths"} <= sb.capabilities.enforced
        result = await sb.run(["python", "job.py"])
        assert result.ok and result.not_applied == []
        spec = runner.specs[0]
        assert spec.argv[0] == "/usr/bin/bwrap" and "--unshare-all" in spec.argv
        assert spec.env == {"PATH": "/usr/bin:/bin"}  # credentials never reach bwrap
        assert spec.memory_mb == 2048
        assert any("seccomp" in result_detail for result_detail in [sb.capabilities.detail])
    finally:
        sb.close()


@pytest.mark.skipif(shutil.which("bwrap") is None or not sys.platform.startswith("linux"),
                    reason="bubblewrap not installed")
async def test_bubblewrap_really_blocks_network_and_protected_writes(tmp_path: Path) -> None:
    sb = BubblewrapSandbox(SandboxPolicy(), root=tmp_path)
    try:
        sb.write_text("probe.py", (
            "import socket\n"
            "try:\n socket.create_connection(('1.1.1.1', 80), timeout=2)\n print('net')\n"
            "except OSError:\n print('nonet')\n"
            "try:\n open('.git/x','w')\n print('wrote')\nexcept OSError:\n print('ro')\n"))
        result = await sb.run(["python", "probe.py"])
        if result.exit_code != 0 and "namespace" in result.stderr:
            pytest.skip("user namespaces unavailable here")
        assert "nonet" in result.stdout and "ro" in result.stdout
    finally:
        sb.close()


# -- W4.1 Landlock / W4.3 strict vs best effort -------------------------------------------------
def test_landrun_command_line(tmp_path: Path) -> None:
    argv = landrun_argv("landrun", SandboxPolicy(), tmp_path, ["python", "x.py"],
                        {"HOME": "/w"}, abi=6, read_paths=["/opt/venv"])
    j = " ".join(argv)
    assert "--best-effort" in argv  # AIR checks the ABI itself
    assert f"--rw {tmp_path}" in j and "--rox /opt/venv" in j and "--env HOME=/w" in j
    assert "--unrestricted-network" not in argv and "--connect-tcp" not in argv
    proxied = landrun_argv("landrun", SandboxPolicy(network=NetworkMode.PROXY), tmp_path,
                           ["python"], {}, abi=10, proxy_port=4000)
    assert "--best-effort" not in proxied and "--connect-tcp 4000" in " ".join(proxied)


def test_strict_mode_refuses_what_landlock_cannot_enforce_and_best_effort_records_it(
        tmp_path: Path) -> None:
    needs = ("filesystem", "network", "protected_paths")
    with pytest.raises(SandboxError) as exc:
        LandlockSandbox(SandboxPolicy(require_enforced=needs), landrun="/bin/landrun", abi=6,
                        runner=FakeRunner(), root=tmp_path)
    problems = " ".join(exc.value.details["problems"])
    assert "network" in problems and "protected_paths" in problems
    assert "filesystem" not in problems

    sb = LandlockSandbox(SandboxPolicy(require_enforced=needs,
                                       mode=EnforcementMode.BEST_EFFORT),
                         landrun="/bin/landrun", abi=6, runner=FakeRunner(), root=tmp_path)
    try:
        assert sb.not_applied == ["network", "protected_paths"]
        assert sb.capabilities.landlock_abi == 6
        assert any("UDP needs ABI v10" in a for a in sb.capabilities.applied)
    finally:
        sb.close()
    full = LandlockSandbox(SandboxPolicy(require_enforced=("filesystem", "network")),
                           landrun="/bin/landrun", abi=10, runner=FakeRunner(), root=tmp_path)
    full.close()


async def test_best_effort_results_list_controls_not_applied(tmp_path: Path) -> None:
    sb = LocalProcessSandbox(SandboxPolicy(require_enforced=("filesystem", "time"),
                                           mode=EnforcementMode.BEST_EFFORT),
                             runner=FakeRunner(), root=tmp_path)
    try:
        result = await sb.run(["python", "x.py"])
        assert result.not_applied == ["filesystem"] and "filesystem" in result.unenforced
    finally:
        sb.close()
    with pytest.raises(SandboxError, match="cannot honour"):
        LocalProcessSandbox(SandboxPolicy(require_enforced=("filesystem",)), root=tmp_path)


# -- W4.1 Seatbelt --------------------------------------------------------------------------------
def test_seatbelt_profile_denies_by_default_and_protects_git() -> None:
    tmp_path = PurePosixPath("/private/var/folders/x/air-sbx-1")
    profile = seatbelt_profile(SandboxPolicy(deny_read=("/Users/me/.ssh",)), tmp_path)
    lines = profile.splitlines()
    assert lines[:2] == ["(version 1)", "(deny default)"]
    assert f'(allow file-write* (subpath "{tmp_path}") (literal "/dev/null"))' in lines
    assert f'(deny file-write* (subpath "{tmp_path / ".git"}"))' in lines
    assert '(deny file-read* (subpath "/Users/me/.ssh"))' in lines
    assert not any("network" in ln for ln in lines)  # no network at all
    # later rules win in SBPL: the .git deny must come after the workspace allow
    assert lines.index(f'(deny file-write* (subpath "{tmp_path / ".git"}"))') > \
        lines.index(f'(allow file-write* (subpath "{tmp_path}") (literal "/dev/null"))')
    proxied = seatbelt_profile(SandboxPolicy(network=NetworkMode.PROXY), tmp_path,
                               proxy_port=5555)
    assert '(allow network-outbound (remote ip "localhost:5555"))' in proxied
    assert "(allow network*)" in seatbelt_profile(SandboxPolicy(network=NetworkMode.ANY),
                                                  tmp_path)


async def test_seatbelt_adapter_wraps_with_sandbox_exec(tmp_path: Path) -> None:
    runner = FakeRunner()
    sb = SeatbeltSandbox(SandboxPolicy(require_enforced=("filesystem", "network")),
                         sandbox_exec="/usr/bin/sandbox-exec", runner=runner, root=tmp_path)
    try:
        assert "memory" not in sb.capabilities.enforced  # macOS ignores RLIMIT_AS
        await sb.run(["python", "x.py"])
        argv = runner.specs[0].argv
        assert argv[:2] == ["/usr/bin/sandbox-exec", "-p"] and "(deny default)" in argv[2]
        assert runner.specs[0].memory_mb is None
    finally:
        sb.close()


@pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="macOS Seatbelt not present")
async def test_seatbelt_really_blocks_protected_writes(tmp_path: Path) -> None:
    sb = SeatbeltSandbox(SandboxPolicy(), root=tmp_path)
    try:
        sb.write_text("w.py", "try:\n open('.git','w')\n print('wrote')\nexcept OSError:\n"
                      " print('ro')\n")
        assert "ro" in (await sb.run(["python", "w.py"])).stdout
    finally:
        sb.close()


# -- W4.1 Windows job objects ---------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
async def test_windows_job_object_enforces_memory(tmp_path: Path) -> None:
    sb = LocalProcessSandbox(SandboxPolicy(max_memory_mb=200, require_enforced=(
        "memory", "cpu", "time")), root=tmp_path)
    try:
        assert {"memory", "cpu"} <= sb.capabilities.enforced
        sb.write_text("hog.py", "x = bytearray(600 * 2**20)\nprint('allocated')\n")
        result = await sb.run(["python", "hog.py"])
        assert "allocated" not in result.stdout and not result.ok
        assert any(a.startswith("job:job_memory") for a in result.applied)
        sb.write_text("ok.py", "print('fine')")
        assert (await sb.run(["python", "ok.py"])).stdout.strip() == "fine"
    finally:
        sb.close()


def test_probe_lists_adapters_and_what_they_enforce() -> None:
    rows = {r.name: r for r in probe_adapters()}
    assert set(rows) == {"container", "bubblewrap", "seatbelt", "landlock", "local"}
    local = rows["local"]
    assert local.available and local.honours_policy and local.capabilities is not None
    assert "filesystem" in local.capabilities.unenforced()


def test_protected_paths_cannot_be_written_through_the_workspace_api(tmp_path: Path) -> None:
    sb = LocalProcessSandbox(root=tmp_path)
    try:
        with pytest.raises(SandboxError, match="protected"):
            sb.write_text(".git/hooks/pre-commit", "evil")
        sb.write_text("ok.txt", "fine")
    finally:
        sb.close()


# -- W4.2 egress policy ----------------------------------------------------------------------
@pytest.mark.parametrize("addr,why", [
    ("10.1.2.3", "private"), ("192.168.1.1", "private"), ("172.20.0.1", "private"),
    ("127.0.0.1", "loopback"), ("::1", "loopback"), ("169.254.169.254", "metadata"),
    ("169.254.10.1", "link_local"), ("fe80::1", "link_local"), ("100.64.0.1", "cgnat"),
    ("fd00:ec2::254", "metadata"), ("::ffff:10.0.0.1", "private"),
    ("64:ff9b::a9fe:a9fe", "metadata"), ("0.0.0.0", "unspecified"), ("8.8.8.8", None)])
def test_default_denied_ranges(addr: str, why: str | None) -> None:
    assert denied_range(addr) == why


def test_domains_are_pinned_at_start_and_rebinding_is_refused() -> None:
    table = {"api.example.com": ["93.184.216.34"], "evil.example.net": ["10.0.0.5"],
             "cdn.example.org": ["151.101.1.1"]}
    d = EgressDecider(EgressPolicy(allow_domains=("api.example.com", "evil.example.net",
                                                  "*.example.org"),
                                   deny_domains=("blocked.example.org",)),
                      fake_resolver(table))
    d.start()
    assert d.pins == {"api.example.com": ("93.184.216.34",), "evil.example.net": ("10.0.0.5",)}
    table["api.example.com"] = ["127.0.0.1"]  # rebinding after start has no effect
    ok = d.decide("api.example.com", 443)
    assert ok.allowed and ok.addresses == ("93.184.216.34",)
    assert "rebinding" in d.decide("evil.example.net", 443).reason
    assert d.decide("cdn.example.org", 443).allowed  # wildcard pinned on first use
    assert not d.decide("example.org", 443).allowed  # *.x does not match x itself
    assert d.decide("blocked.example.org", 443).reason == "domain is denied"
    assert not d.decide("other.com", 443).allowed
    assert not d.decide("api.example.com", 22).allowed  # port not allowed
    assert not d.decide("8.8.8.8", 443).allowed  # raw addresses need an explicit rule
    assert not d.decide("169.254.169.254", 80).allowed


def test_explicit_address_rules_open_private_ranges_but_not_metadata_by_accident() -> None:
    d = EgressDecider(EgressPolicy(allow_cidrs=("127.0.0.0/8", "169.254.0.0/16"),
                                   allow_ports=(80,)))
    assert d.decide("127.0.0.1", 80).allowed
    assert not d.decide("169.254.169.254", 80).allowed  # needs a rule naming exactly it
    d2 = EgressDecider(EgressPolicy(allow_cidrs=("169.254.169.254/32",), allow_ports=(80,)))
    assert d2.decide("169.254.169.254", 80).allowed
    with pytest.raises(EgressError):
        EgressDecider(EgressPolicy(allow_cidrs=("not-a-cidr",)))


def test_policy_validation() -> None:
    with pytest.raises(SandboxError, match="invalid sandbox policy"):
        SandboxPolicy(egress=EgressPolicy(secrets=(SecretBinding("K", "SRC", ("h",)),)))
    with pytest.raises(SandboxError):
        SandboxPolicy(protected_paths=("../outside",))
    p = SandboxPolicy(network=NetworkMode.PROXY, egress=EgressPolicy(
        allow_domains=("pypi.org",), secrets=(SecretBinding("TOKEN", "HOST_TOKEN",
                                                            ("pypi.org",)),)))
    assert SandboxPolicy.from_dict(json.loads(p.to_json())) == p
    old = {"$schema": "ar.sandbox_policy/1.0", "network": "none"}  # 1.0 documents still load
    assert SandboxPolicy.from_dict(old).mode is EnforcementMode.STRICT


class _Upstream(http.server.BaseHTTPRequestHandler):
    seen: list[dict[str, str]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        _Upstream.seen.append({k.lower(): v for k, v in self.headers.items()})
        body = json.dumps({"auth": self.headers.get("Authorization", "")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def upstream() -> Iterator[int]:
    _Upstream.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield int(server.server_address[1])
    server.shutdown()
    server.server_close()


def _get(proxy_port: int, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    conn.request("GET", url, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.read()


def test_proxy_substitutes_secrets_only_for_allowed_hosts_and_headers(upstream: int) -> None:
    table = {"api.test": ["127.0.0.1"], "other.test": ["127.0.0.1"]}
    policy = EgressPolicy(allow_domains=("api.test", "other.test"), allow_cidrs=("127.0.0.1/32",),
                          allow_ports=(upstream,),
                          secrets=(SecretBinding("API_TOKEN", "HOST_API_TOKEN", ("api.test",),
                                                 allow_plain_http=True),))
    proxy = EgressProxy(policy, fake_resolver(table), {"HOST_API_TOKEN": "s3cr3t-value"})
    port = proxy.start_tcp()
    try:
        env = proxy.sandbox_env()
        ph = env["API_TOKEN"]
        assert "s3cr3t" not in ph and env["HTTP_PROXY"] == f"http://127.0.0.1:{port}"
        status, body = _get(port, f"http://api.test:{upstream}/v1",
                            {"Authorization": f"Bearer {ph}"})
        assert status == 200
        assert _Upstream.seen[-1]["authorization"] == "Bearer s3cr3t-value"
        assert b"s3cr3t" not in body and ph.encode() in body  # echoed secret is redacted
        # the same placeholder to a host the secret is not bound to: blocked, never sent
        n = len(_Upstream.seen)
        status, body = _get(port, f"http://other.test:{upstream}/x",
                            {"Authorization": f"Bearer {ph}"})
        assert status == 403 and b"may not be sent" in body and len(_Upstream.seen) == n
        # placeholder outside the allowed header fields: blocked
        status, _ = _get(port, f"http://api.test:{upstream}/x?k={ph}", {})
        assert status == 403
        # a host outside the allowlist, and a metadata address: denied
        assert _get(port, "http://elsewhere.test/", {})[0] == 403
        assert _get(port, "http://169.254.169.254/latest/meta-data/", {})[0] == 403
        events = proxy.decider.events
        assert [e.allowed for e in events] == [True, False, False, False, False]
        assert events[0].substituted == ("API_TOKEN",) and events[0].address == "127.0.0.1"
    finally:
        proxy.close()


def test_secrets_need_tls_upstream_unless_opted_in(upstream: int) -> None:
    policy = EgressPolicy(allow_domains=("api.test",), allow_cidrs=("127.0.0.1/32",),
                          allow_ports=(upstream,),
                          secrets=(SecretBinding("T", "SRC", ("api.test",)),))
    proxy = EgressProxy(policy, fake_resolver({"api.test": ["127.0.0.1"]}), {"SRC": "v"})
    port = proxy.start_tcp()
    try:
        ph = proxy.sandbox_env()["T"]
        status, body = _get(port, f"http://api.test:{upstream}/", {"Authorization": ph})
        assert status == 403 and b"requires a TLS upstream" in body and not _Upstream.seen
        # no placeholder: plain requests to allowed hosts pass
        assert _get(port, f"http://api.test:{upstream}/", {})[0] == 200
    finally:
        proxy.close()
    with pytest.raises(EgressError, match="not set"):
        EgressProxy(policy, environ={})


def test_connect_tunnels_follow_the_same_policy(upstream: int) -> None:
    policy = EgressPolicy(allow_domains=("api.test",), allow_cidrs=("127.0.0.1/32",),
                          allow_ports=(upstream,))
    proxy = EgressProxy(policy, fake_resolver({"api.test": ["127.0.0.1"]}))
    port = proxy.start_tcp()
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(f"CONNECT api.test:{upstream} HTTP/1.1\r\nHost: api.test\r\n\r\n".encode())
        assert b" 200 " in s.recv(4096)
        s.sendall(b"GET / HTTP/1.1\r\nHost: api.test\r\nConnection: close\r\n\r\n")
        data = b""
        while chunk := s.recv(4096):
            data += chunk
        assert b"200 OK" in data
        s.close()
        s = socket.create_connection(("127.0.0.1", port), timeout=10)
        s.sendall(b"CONNECT 10.0.0.1:443 HTTP/1.1\r\n\r\n")
        assert b" 403 " in s.recv(4096)
        s.close()
    finally:
        proxy.close()


async def test_proxied_local_sandbox_attributes_violations_to_the_command(
        tmp_path: Path, upstream: int) -> None:
    policy = SandboxPolicy(network=NetworkMode.PROXY, egress=EgressPolicy(
        allow_domains=("api.test",), allow_cidrs=("127.0.0.1/32",), allow_ports=(upstream,)))
    sb = LocalProcessSandbox(policy, root=tmp_path,
                             resolver=fake_resolver({"api.test": ["127.0.0.1"]}))
    try:
        assert "network" in sb.capabilities.unenforced()  # a process can ignore the proxy
        sb.write_text("fetch.py", (
            "import urllib.request\n"
            f"for url in ['http://api.test:{upstream}/', 'http://denied.test/']:\n"
            "    try:\n        urllib.request.urlopen(url, timeout=10); print('ok', url)\n"
            "    except Exception as e:\n        print('err', url, e)\n"))
        result = await sb.run(["python", "fetch.py"])
        assert result.ok, result.stderr
        assert [e["allowed"] for e in result.egress] == [True, False]
        assert result.violations[0]["host"] == "denied.test"
        again = await sb.run(["python", "-c", "print(1)"])
        assert again.egress == []  # events belong to the run that made them
    finally:
        sb.close()


# -- W4.4 remote sandboxes ------------------------------------------------------------------------
async def test_remote_sandbox_records_placement_and_syncs_the_workspace(tmp_path: Path) -> None:
    client = FakeRemoteClient()
    sb = RemoteSandbox(client, SandboxPolicy(), root=tmp_path)
    try:
        assert sb.capabilities.placement is Placement.CLOUD
        assert sb.capabilities.data_leaves_device
        sb.write_text("job.py", "open('out.txt','w').write('42')\nprint('done')\n")
        result = await sb.run(["python", "job.py"])
        assert result.ok and result.placement is Placement.CLOUD
        assert sb.read_text("out.txt") == "42"  # pulled back
        with pytest.raises(SandboxError, match="not allowed"):
            await sb.run(["bash", "-c", "true"])
    finally:
        sb.close()
    assert client.calls[0] == "create fake-1" and client.calls[-1] == "destroy fake-1"
    with pytest.raises(SandboxError, match="cannot honour"):
        RemoteSandbox(FakeRemoteClient(), SandboxPolicy(require_enforced=("filesystem",)))


@pytest.mark.req("F07")
async def test_restricted_work_never_goes_to_a_remote_sandbox(tmp_path: Path) -> None:
    project = Project.init(tmp_path / "p", requirements=baseline_requirements())
    r = ModelRouter()
    r.register(ScriptedChat([call("list_files"), say("done")],  # type: ignore[list-item]
                            location=Location.LOCAL_HOST))
    agent = WorkAgent(project, r, sandbox_factory=lambda p: RemoteSandbox(FakeRemoteClient(), p))
    task = WorkTask(task_id="r1", kind=WorkKind.RESEARCH, goal="x",
                    data_classification=DataClass.RESTRICTED)
    result = await agent.run(task)
    assert result.status is WorkStatus.SANDBOX_REFUSED
    assert "cloud" in result.problems[0]
    sbx = project.ledger.query(kind="work.sandbox")[0].data
    assert sbx["placement"] == "cloud" and sbx["data_leaves_device"] is True


def test_netbridge_usage_error() -> None:
    from agentic_robotics.work import netbridge

    assert netbridge.main(["only-one"]) == 2
    assert os.path.exists(netbridge.__file__)
