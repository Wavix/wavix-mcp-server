# syntax=docker/dockerfile:1.7
# Pin to a specific digest in production deployments — replace the tag below
# with the @sha256:... digest you have verified, e.g. python:3.13-slim@sha256:...
FROM python:3.13-slim AS builder

WORKDIR /app

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

FROM python:3.13-slim AS runtime

WORKDIR /app

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin mcp

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv

USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import socket,sys; s=socket.socket(); s.settimeout(3); s.connect(('127.0.0.1',8000)); s.close()" || exit 1

ENTRYPOINT ["wavix-mcp"]
