"""In-sandbox bridge: TCP 127.0.0.1:PORT -> a bind-mounted Unix socket (the egress proxy).

Run *inside* a bubblewrap sandbox whose network namespace is empty (``--unshare-net``)::

    python -I netbridge.py /run/air-proxy/proxy.sock 3128 -- python job.py

The only route out of the namespace is this bridge, and the only thing it reaches is the
host-side :class:`~agentic_robotics.work.egress.EgressProxy`, which applies the egress
policy. Pattern from anthropics/sandbox-runtime and openai/codex linux-sandbox (both
Apache-2.0: proxy on a Unix socket bridged into an isolated namespace); no code copied.
Standard library only: it is executed by file path with ``python -I``.
"""

from __future__ import annotations

import select
import socket
import subprocess
import sys
import threading

__requirements__ = ("AIR01", "REQ02", "G15")


def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            ready, _, _ = select.select([a, b], [], [], 300)
            if not ready:
                return
            for s in ready:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        return
    finally:
        a.close()
        b.close()


def _serve(listener: socket.socket, uds: str) -> None:
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        up = socket.socket(getattr(socket, "AF_UNIX"), socket.SOCK_STREAM)  # noqa: B009
        try:
            up.connect(uds)
        except OSError:
            conn.close()
            up.close()
            continue
        threading.Thread(target=_pipe, args=(conn, up), daemon=True).start()


def main(argv: list[str]) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: netbridge.py UDS PORT -- COMMAND...", file=sys.stderr)
        return 2
    uds, port, cmd = argv[0], int(argv[1]), argv[3:]
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(64)
    threading.Thread(target=_serve, args=(listener, uds), daemon=True).start()
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
