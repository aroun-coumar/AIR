"""``python -m agentic_robotics.engineering.serve <backend>``: serve a registered backend
over the ``air.backend/1`` JSON-lines protocol on stdin/stdout."""

from __future__ import annotations

import sys

from .backends import make_backend
from .process import serve

__requirements__ = ("REQ04",)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0].startswith("process:"):
        print("usage: python -m agentic_robotics.engineering.serve <backend>",
              file=sys.stderr)
        return 2
    serve(make_backend(args[0]), sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
