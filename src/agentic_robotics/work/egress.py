"""Egress policy as data, and the stdlib proxy that enforces it.

A sandbox whose policy says ``network: proxy`` may reach the outside only through an
:class:`EgressProxy` (HTTP forward proxy plus ``CONNECT`` tunnels). The proxy decides each
connection with an :class:`EgressDecider`:

* **Default deny inward.** Private (RFC 1918, CGNAT, IPv6 ULA), loopback, link-local,
  unspecified, multicast and reserved addresses, and the cloud metadata endpoints, are
  refused unless an explicit address rule (``allow_cidrs``) opens them. Metadata
  addresses are opened only by a rule naming exactly that address.
* **Domain allowlist, DNS-pinned.** Exact allowed domains are resolved when the proxy
  starts; wildcard domains on first use. The proxy then dials only the pinned addresses
  and never re-resolves, so a domain cannot be rebound to an internal address later.
  Answers that fall in a denied range are dropped (rebinding protection).
* **Secrets as placeholders.** The sandbox environment holds a random placeholder, not
  the credential. The real value is substituted only in an allowed header of a request
  the proxy can see (absolute-form HTTP), going to a host the secret is bound to, and by
  default only when the proxy itself opens TLS to that host (``tls_origination``). A
  placeholder seen anywhere else blocks the request. Opaque ``CONNECT`` tunnels never
  receive substitutions.

Every decision is an :class:`EgressEvent`; a sandbox attributes the events of each run to
that command. Patterns adopted from superradcompany/microsandbox (Apache-2.0) network and
secrets design and anthropics/sandbox-runtime (Apache-2.0) proxy filtering; no code copied.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import os
import secrets as _secrets
import select
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

from ..core.errors import FrameworkError

__requirements__ = ("AIR01", "REQ02", "REQ04", "G15")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str], list[str]]


class EgressError(FrameworkError):
    code = "egress_error"


# -- policy data -----------------------------------------------------------------------------
@dataclass(frozen=True)
class SecretBinding:
    """A credential the sandbox may *use* but never *hold*.

    ``env`` is the variable name inside the sandbox (it receives a placeholder);
    ``source_env`` names the host variable holding the real value (the value itself is
    never written into a policy). ``hosts`` are the host patterns the real value may be
    sent to; ``header_fields`` the only request headers it may appear in."""

    env: str
    source_env: str
    hosts: tuple[str, ...]
    header_fields: tuple[str, ...] = ("authorization",)
    allow_plain_http: bool = False   # substitute even when the upstream is not TLS (weaker)


@dataclass(frozen=True)
class EgressPolicy:
    """Where a proxied sandbox may connect. Everything not allowed is denied."""

    allow_domains: tuple[str, ...] = ()   # "api.example.com", "*.example.org"
    deny_domains: tuple[str, ...] = ()    # checked first
    allow_cidrs: tuple[str, ...] = ()     # explicit address rules; may open private ranges
    allow_ports: tuple[int, ...] = (80, 443)
    tls_origination: tuple[str, ...] = ()  # hosts the proxy dials with TLS for plain requests
    secrets: tuple[SecretBinding, ...] = ()

    def problems(self) -> list[str]:
        out = []
        for c in self.allow_cidrs:
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError:
                out.append(f"invalid CIDR in allow_cidrs: {c!r}")
        for p in self.allow_ports:
            if not 0 < p < 65536:
                out.append(f"invalid port {p}")
        for s in self.secrets:
            if not s.hosts:
                out.append(f"secret {s.env!r} is bound to no host")
            if not s.header_fields:
                out.append(f"secret {s.env!r} has no header field to be substituted in")
        return out


# -- address classification -------------------------------------------------------------------
_METADATA = (ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"),
             ipaddress.ip_address("100.100.100.200"))
_DENIED_RANGES: tuple[tuple[str, str], ...] = (
    ("0.0.0.0/8", "unspecified"), ("10.0.0.0/8", "private"), ("100.64.0.0/10", "cgnat"),
    ("127.0.0.0/8", "loopback"), ("169.254.0.0/16", "link_local"),
    ("172.16.0.0/12", "private"), ("192.0.0.0/24", "reserved"), ("192.168.0.0/16", "private"),
    ("198.18.0.0/15", "reserved"), ("224.0.0.0/4", "multicast"), ("240.0.0.0/4", "reserved"),
    ("::/128", "unspecified"), ("::1/128", "loopback"), ("fc00::/7", "private"),
    ("fe80::/10", "link_local"), ("ff00::/8", "multicast"))
_NETS = tuple((ipaddress.ip_network(n), why) for n, why in _DENIED_RANGES)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _unwrap(ip: IPAddress) -> IPAddress:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return ip.ipv4_mapped
        if ip in _NAT64:
            return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return ip


def denied_range(address: str | IPAddress) -> str | None:
    """Why ``address`` is denied by default (``None`` if it is a public address)."""
    ip = _unwrap(ipaddress.ip_address(address) if isinstance(address, str) else address)
    if ip in _METADATA:
        return "metadata"
    for net, why in _NETS:
        if ip.version == net.version and ip in net:
            return why
    return None


def _host_matches(host: str, patterns: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for p in patterns:
        p = p.lower().rstrip(".")
        if p.startswith("*."):
            if host.endswith(p[1:]) and host != p[2:]:
                return True
        elif fnmatch.fnmatchcase(host, p):
            return True
    return False


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def system_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if addr not in out:
            out.append(addr)
    return out


# -- decisions --------------------------------------------------------------------------------
@dataclass(frozen=True)
class EgressEvent:
    seq: int
    at: float
    method: str
    host: str
    port: int
    allowed: bool
    reason: str
    address: str | None = None
    substituted: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "method": self.method, "host": self.host, "port": self.port,
                "allowed": self.allowed, "reason": self.reason, "address": self.address,
                "substituted": list(self.substituted)}


@dataclass(frozen=True)
class EgressDecision:
    allowed: bool
    reason: str
    addresses: tuple[str, ...] = ()


class EgressDecider:
    """Decides connections for one sandbox session and keeps the event log."""

    def __init__(self, policy: EgressPolicy, resolver: Resolver = system_resolver) -> None:
        problems = policy.problems()
        if problems:
            raise EgressError("invalid egress policy", problems=problems)
        self.policy = policy
        self.resolver = resolver
        self._cidrs = [ipaddress.ip_network(c, strict=False) for c in policy.allow_cidrs]
        self.pins: dict[str, tuple[str, ...]] = {}
        self.events: list[EgressEvent] = []
        self._lock = threading.Lock()

    def start(self) -> None:
        """Pin every exact allowed domain to the addresses it resolves to now."""
        for d in self.policy.allow_domains:
            if "*" not in d and "?" not in d and not _is_ip(d):
                self._pin(d.lower())

    def _pin(self, host: str) -> tuple[str, ...]:
        with self._lock:
            if host in self.pins:
                return self.pins[host]
        try:
            answers = tuple(self.resolver(host))
        except OSError:
            answers = ()
        with self._lock:
            return self.pins.setdefault(host, answers)

    def _explicitly_allowed(self, ip: IPAddress) -> bool:
        ip = _unwrap(ip)
        for net in self._cidrs:
            if ip.version != net.version or ip not in net:
                continue
            if ip in _METADATA and net.num_addresses != 1:
                continue  # metadata only by a rule naming exactly that address
            return True
        return False

    def _address_ok(self, address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return self._explicitly_allowed(ip) or denied_range(ip) is None

    def decide(self, host: str, port: int) -> EgressDecision:
        host = host.strip("[]").lower().rstrip(".")
        if port not in self.policy.allow_ports:
            return EgressDecision(False, f"port {port} is not allowed")
        if _is_ip(host):
            ip = ipaddress.ip_address(host)
            if self._explicitly_allowed(ip):
                return EgressDecision(True, "address allowed by an explicit rule", (host,))
            why = denied_range(ip)
            return EgressDecision(False, f"raw address not allowed ({why or 'not in allow_cidrs'})")
        if _host_matches(host, self.policy.deny_domains):
            return EgressDecision(False, "domain is denied")
        if not _host_matches(host, self.policy.allow_domains):
            return EgressDecision(False, "domain is not in the allowlist")
        answers = self._pin(host)
        good = tuple(a for a in answers if self._address_ok(a))
        if not good:
            why = "does not resolve" if not answers else \
                "resolves only to denied ranges (rebinding protection)"
            return EgressDecision(False, f"domain {why}")
        return EgressDecision(True, "domain allowed (pinned)", good)

    def record(self, method: str, host: str, port: int, decision: EgressDecision,
               address: str | None = None, substituted: Iterable[str] = (),
               reason: str | None = None) -> EgressEvent:
        with self._lock:
            ev = EgressEvent(len(self.events) + 1, time.time(), method, host, port,
                             decision.allowed, reason or decision.reason, address,
                             tuple(substituted))
            self.events.append(ev)
            return ev


# -- secrets ----------------------------------------------------------------------------------
class SecretVault:
    """Real values stay here (host memory); the sandbox only sees placeholders."""

    def __init__(self, bindings: Iterable[SecretBinding],
                 environ: dict[str, str] | None = None) -> None:
        env = os.environ if environ is None else environ
        self.bindings = tuple(bindings)
        self.placeholders: dict[str, str] = {}
        self._values: dict[str, str] = {}
        for b in self.bindings:
            value = env.get(b.source_env)
            if value is None:
                raise EgressError("secret source variable is not set", secret=b.env,
                                  source_env=b.source_env)
            ph = f"air-secret-{b.env.lower()}-{_secrets.token_hex(12)}"
            self.placeholders[b.env] = ph
            self._values[ph] = value

    def sandbox_env(self) -> dict[str, str]:
        return dict(self.placeholders)

    def binding_for(self, placeholder: str) -> SecretBinding:
        env = next(k for k, v in self.placeholders.items() if v == placeholder)
        return next(b for b in self.bindings if b.env == env)

    def find(self, text: str) -> list[str]:
        return [ph for ph in self._values if ph in text]

    def value(self, placeholder: str) -> str:
        return self._values[placeholder]

    def redact(self, data: bytes) -> bytes:
        for ph, value in self._values.items():
            if value:
                data = data.replace(value.encode(), ph.encode())
        return data


# -- the proxy --------------------------------------------------------------------------------
_HOP = {"connection", "proxy-connection", "keep-alive", "proxy-authorization", "te",
        "trailer", "transfer-encoding", "upgrade"}
MAX_BODY = 32 * 2**20


def _relay(a: socket.socket, b: socket.socket, idle_s: float = 60.0) -> None:
    socks = [a, b]
    try:
        while True:
            ready, _, _ = select.select(socks, [], [], idle_s)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return


class _Handler(BaseHTTPRequestHandler):
    server: Any
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _deny(self, status: int, reason: str) -> None:
        body = f"AIR egress policy: {reason}\n".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    @property
    def proxy(self) -> EgressProxy:
        return self.server.air_proxy  # type: ignore[no-any-return]

    def do_CONNECT(self) -> None:
        host, _, port_s = self.path.rpartition(":")
        port = int(port_s) if port_s.isdigit() else 443
        decider = self.proxy.decider
        decision = decider.decide(host, port)
        if not decision.allowed:
            decider.record("CONNECT", host, port, decision)
            self._deny(403, decision.reason)
            return
        try:
            upstream = self.proxy.dial(decision.addresses, port)
        except OSError as exc:
            decider.record("CONNECT", host, port, EgressDecision(False, f"connect failed: {exc}"))
            self._deny(502, "upstream connection failed")
            return
        decider.record("CONNECT", host, port, decision, address=upstream.getpeername()[0])
        self.send_response(200, "Connection established")
        self.end_headers()
        self.wfile.flush()
        with upstream:
            _relay(self.connection, upstream)
        self.close_connection = True

    def _forward(self) -> None:
        method = self.command
        parts = urlsplit(self.path)
        if parts.scheme != "http" or not parts.hostname:
            self._deny(400, "only absolute-form http:// requests and CONNECT are proxied")
            return
        host = parts.hostname
        port = parts.port or 80
        decider = self.proxy.decider
        tls = _host_matches(host, decider.policy.tls_origination)
        upstream_port = parts.port or (443 if tls else 80)
        decision = decider.decide(host, upstream_port)
        if not decision.allowed:
            decider.record(method, host, upstream_port, decision)
            self._deny(403, decision.reason)
            return
        if self.headers.get("Transfer-Encoding", "").lower() not in ("", "identity"):
            decider.record(method, host, port, EgressDecision(False, "chunked bodies unsupported"))
            self._deny(411, "send a Content-Length body")
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._deny(413, "request body too large")
            return
        body = self.rfile.read(length) if length else b""
        target = parts.path or "/"
        if parts.query:
            target += "?" + parts.query
        headers = [(k, v) for k, v in self.headers.items() if k.lower() not in _HOP]
        vault = self.proxy.vault
        substituted: list[str] = []
        if vault is not None:
            blocked = None
            for loc, text in [("url", target), ("body", body.decode("latin-1")),
                              *[(f"header:{k.lower()}", v) for k, v in headers]]:
                for ph in vault.find(text):
                    b = vault.binding_for(ph)
                    if not _host_matches(host, b.hosts):
                        blocked = f"secret {b.env} may not be sent to {host}"
                    elif loc.removeprefix("header:") not in {h.lower() for h in b.header_fields}:
                        blocked = f"secret {b.env} appears outside its allowed header fields"
                    elif not tls and not b.allow_plain_http:
                        blocked = f"secret {b.env} requires a TLS upstream"
                    else:
                        substituted.append(b.env)
            if blocked:
                decider.record(method, host, upstream_port, EgressDecision(False, blocked))
                self._deny(403, blocked)
                return
            headers = [(k, _sub(vault, v)) for k, v in headers]
        try:
            sock = self.proxy.dial(decision.addresses, upstream_port)
        except OSError as exc:
            decider.record(method, host, upstream_port,
                           EgressDecision(False, f"connect failed: {exc}"))
            self._deny(502, "upstream connection failed")
            return
        address = sock.getpeername()[0]
        try:
            if tls:
                sock = self.proxy.tls_context.wrap_socket(sock, server_hostname=host)
            status, reason, resp_headers, resp_body = _exchange(sock, method, target, headers,
                                                                body)
        except (OSError, ssl.SSLError) as exc:
            decider.record(method, host, upstream_port,
                           EgressDecision(False, f"upstream failed: {exc}"), address=address)
            self._deny(502, "upstream request failed")
            return
        finally:
            sock.close()
        decider.record(method, host, upstream_port, decision, address=address,
                       substituted=substituted)
        if vault is not None:
            resp_body = vault.redact(resp_body)
            resp_headers = [(k, vault.redact(v.encode("latin-1")).decode("latin-1"))
                            for k, v in resp_headers]
        self.send_response(status, reason)
        for k, v in resp_headers:
            if k.lower() not in _HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(resp_body)
        self.close_connection = True

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


def _sub(vault: SecretVault, text: str) -> str:
    for ph in vault.find(text):
        text = text.replace(ph, vault.value(ph))
    return text


def _exchange(sock: socket.socket, method: str, target: str, headers: list[tuple[str, str]],
              body: bytes) -> tuple[int, str, list[tuple[str, str]], bytes]:
    import http.client

    conn = http.client.HTTPConnection("upstream")
    conn.sock = sock
    conn.putrequest(method, target, skip_host=True, skip_accept_encoding=True)
    for k, v in headers:
        conn.putheader(k, v)
    if body or method in ("POST", "PUT", "PATCH"):
        conn.putheader("Content-Length", str(len(body)))
    conn.putheader("Connection", "close")
    conn.endheaders(body or None)
    resp = conn.getresponse()
    data = resp.read(MAX_BODY) if method != "HEAD" else b""
    return resp.status, resp.reason, list(resp.getheaders()), data


class _TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


@dataclass
class EgressProxy:
    """A forward proxy enforcing an :class:`EgressPolicy` for one sandbox session.

    Listens on ``127.0.0.1`` (TCP) or, on POSIX, a Unix socket that can be bind-mounted
    into a sandbox whose network namespace is otherwise empty."""

    policy: EgressPolicy
    resolver: Resolver = system_resolver
    environ: dict[str, str] | None = None
    connect_timeout_s: float = 10.0
    tls_context: ssl.SSLContext = field(default_factory=ssl.create_default_context)
    decider: EgressDecider = field(init=False)
    vault: SecretVault | None = field(init=False, default=None)
    _server: socketserver.BaseServer | None = field(init=False, default=None)
    _thread: threading.Thread | None = field(init=False, default=None)
    address: str = field(init=False, default="")

    def __post_init__(self) -> None:
        self.decider = EgressDecider(self.policy, self.resolver)
        if self.policy.secrets:
            self.vault = SecretVault(self.policy.secrets, self.environ)

    def dial(self, addresses: Iterable[str], port: int) -> socket.socket:
        last: OSError | None = None
        for a in addresses:
            try:
                return socket.create_connection((a, port), timeout=self.connect_timeout_s)
            except OSError as exc:
                last = exc
        raise last or OSError("no address to dial")

    def _serve(self, server: socketserver.BaseServer) -> None:
        server.air_proxy = self  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, name="air-egress-proxy",
                                        daemon=True)
        self._thread.start()

    def start_tcp(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self.decider.start()
        server = _TCPServer((host, port), _Handler)
        self._serve(server)
        bound = int(server.server_address[1])
        self.address = f"http://{host}:{bound}"
        return bound

    def start_unix(self, path: str) -> str:
        unix_server = getattr(socketserver, "ThreadingUnixStreamServer", None)
        if unix_server is None:
            raise EgressError("Unix-socket proxy needs a POSIX host")
        self.decider.start()

        class _UnixServer(unix_server):  # type: ignore[misc, valid-type]
            daemon_threads = True

            def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
                conn, _ = self.socket.accept()
                return conn, ("unix", 0)

        self._serve(_UnixServer(path, _Handler))
        self.address = f"unix:{path}"
        return path

    def sandbox_env(self, proxy_url: str | None = None) -> dict[str, str]:
        url = proxy_url or self.address
        env = {k: url for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
                                "ALL_PROXY", "all_proxy")}
        env["NO_PROXY"] = env["no_proxy"] = ""
        if self.vault is not None:
            env.update(self.vault.sandbox_env())
        return env

    def events_since(self, seq: int) -> list[EgressEvent]:
        return [e for e in self.decider.events if e.seq > seq]

    @property
    def last_seq(self) -> int:
        return len(self.decider.events)

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


__all__ = ["EgressDecider", "EgressDecision", "EgressError", "EgressEvent", "EgressPolicy",
           "EgressProxy", "SecretBinding", "SecretVault", "denied_range", "system_resolver"]
