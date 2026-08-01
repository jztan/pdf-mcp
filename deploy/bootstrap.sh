#!/usr/bin/env bash
# First-run setup for the pdf-mcp Docker deployment.
#
# Does only what `docker compose` cannot: generate the auth token and
# create the mount directories. Lifecycle commands are deliberately absent;
# use docker compose up/down/logs/ps directly.
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE=".env.docker"
EXAMPLE_FILE=".env.docker.example"

if [ -f "$ENV_FILE" ]; then
    echo "==> $ENV_FILE already exists, leaving it untouched."
    echo "    Delete it first if you want a fresh token."
else
    if [ ! -f "$EXAMPLE_FILE" ]; then
        echo "!!! $EXAMPLE_FILE is missing; cannot generate $ENV_FILE." >&2
        exit 1
    fi
    if ! command -v openssl >/dev/null 2>&1; then
        echo "!!! openssl not found; cannot generate a token." >&2
        echo "    Copy $EXAMPLE_FILE to $ENV_FILE and set" >&2
        echo "    PDF_MCP_AUTH_TOKEN to a long random secret by hand." >&2
        exit 1
    fi
    TOKEN="$(openssl rand -hex 32)"
    sed "s|^PDF_MCP_AUTH_TOKEN=.*|PDF_MCP_AUTH_TOKEN=${TOKEN}|" \
        "$EXAMPLE_FILE" > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "==> Wrote $ENV_FILE with a generated auth token (mode 600)."
fi

if [ -d "documents" ]; then
    echo "==> ./documents already exists."
else
    mkdir -p documents
    echo "==> Created ./documents. Put the PDFs to serve in here."
fi

echo
echo "Next steps:"
echo "  1. Put PDFs in ./documents"
echo "  2. docker compose up -d --build"
echo "  3. curl -fsS http://127.0.0.1:\${PDF_MCP_HOST_PORT:-8802}/health"
echo
echo "Your auth token (clients send it as 'Authorization: Bearer <token>'):"
grep '^PDF_MCP_AUTH_TOKEN=' "$ENV_FILE"
