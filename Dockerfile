# Reproducible development container for the Agentic Robotics Framework.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

COPY --from=ghcr.io/astral-sh/uv:0.13.4 /uv /usr/local/bin/uv

WORKDIR /workspace
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv venv /opt/venv && \
    VIRTUAL_ENV=/opt/venv uv pip install -e ".[dev]"
ENV PATH="/opt/venv/bin:${PATH}"

COPY . .
CMD ["pytest", "-q"]
