# 0001. Isolated Python 3.12 environment managed by uv

Status: Accepted
Date: 2026-10-22

## Context
The baseline proposes a Python-facing API. The development machine had no Python or
Docker, and the owner asked for everything to live in a container or venv.

## Decision
Use uv to provision a managed CPython 3.12 and a project `.venv`, with nothing installed
system-wide. Ship a `Dockerfile` and `.devcontainer/` that run the same toolchain.
Support Python >= 3.11.

## Consequences
Reproducible setup on any machine with uv. The Docker image has not yet been built on
the development machine.
