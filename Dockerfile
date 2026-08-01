# Multi-stage build for the pdf-mcp HTTP transport.
#
# No platform pinning on purpose: build this on the host that will run it.
# The alternative, pinning linux/amd64 and building on an arm64 Mac, runs
# under QEMU emulation, which is slow and unreliable for native wheels
# like onnxruntime.
FROM python:3.13-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FASTEMBED_CACHE_PATH=/opt/fastembed

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# pyproject references README.md via `readme =` but declares the license
# inline as text, so no LICENSE file is needed at build time.
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

RUN uv venv /opt/venv && \
    uv pip install ".[multicolumn]" --python=/opt/venv/bin/python

# Bake the embedding model so the first semantic query does not stall on a
# ~130 MB download and the container works air-gapped. Without
# FASTEMBED_CACHE_PATH this lands in /tmp and is lost on restart.
RUN /opt/venv/bin/python -c \
    "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

FROM python:3.13-slim AS runtime

# Base image must be glibc, not musl: onnxruntime ships no musl wheels.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/opt/fastembed \
    PDF_MCP_CACHE_DIR=/data/cache \
    PDF_MCP_HTTP_HOST=0.0.0.0 \
    PDF_MCP_HTTP_PORT=8000 \
    PDF_MCP_HTTP_PATH=/mcp

# 0.0.0.0 above is correct and is NOT a weakening of the loopback default.
# A container has its own network namespace, so binding loopback inside
# would make the port unreachable even from the host. The loopback
# guarantee lives in the compose port publish (127.0.0.1:PORT:8000).

# tesseract-ocr pulls tesseract-ocr-eng automatically; curl is for HEALTHCHECK.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tesseract-ocr && \
    rm -rf /var/lib/apt/lists/*

# --create-home is load-bearing: the config bind mount lands at Path.home(),
# which resolves through $HOME / the passwd entry. A bare numeric USER 1000
# would not give /home/appuser.
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

COPY --from=builder --chown=appuser:appuser /opt/venv /opt/venv
COPY --from=builder --chown=appuser:appuser /opt/fastembed /opt/fastembed

# Create and chown BEFORE any volume is mounted. Docker seeds a fresh named
# volume from the ownership of the image directory it covers; that is what
# keeps the SQLite cache writable as uid 1000 on a Linux host.
RUN mkdir -p /data/cache /data/pdfs && \
    chown -R appuser:appuser /data

USER appuser
WORKDIR /app

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["pdf-mcp-http"]
