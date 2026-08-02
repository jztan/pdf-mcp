#!/usr/bin/env bash
# One-command deployment for the pdf-mcp HTTP transport.
#
# Thin wrapper: env setup delegates to deploy/bootstrap.sh and every
# lifecycle verb delegates to docker compose, so docker-compose.yml stays
# the single source of truth for ports, volumes, and service names.
# Nothing here restates a port or a mount.
set -euo pipefail

cd "$(dirname "$0")"

usage() {
    cat <<'EOF'
pdf-mcp Docker deployment

Usage: ./deploy.sh [OPTION]

Options:
    (none)          Full deployment: env setup, build, start, health test
    --build-only    Build the image and exit
    --no-test       Deploy but skip the health test
    --cleanup       Stop and remove the stack (docker compose down)
    --logs          Show recent container logs
    --status        Show container status
    --help          Show this help

The stack publishes to 127.0.0.1 on the port set by PDF_MCP_HOST_PORT
in .env (default 8802). Lifecycle equivalents, usable directly:
    docker compose up -d --build | down | logs -f | ps
EOF
}

check_docker() {
    if ! docker info >/dev/null 2>&1; then
        echo "!!! Docker is not running. Start Docker first." >&2
        exit 1
    fi
}

host_port() {
    local port=""
    if [ -f .env ]; then
        port="$(grep -E '^PDF_MCP_HOST_PORT=' .env | cut -d= -f2 || true)"
    fi
    echo "${port:-8802}"
}

health_test() {
    local port
    port="$(host_port)"
    echo "==> Waiting for http://127.0.0.1:${port}/health ..."
    for _ in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
            echo "==> Health check passed."
            return 0
        fi
        sleep 2
    done
    echo "!!! Health check failed after 60s. Recent logs:" >&2
    docker compose logs --tail 20 >&2
    exit 1
}

case "${1:-}" in
    --help)
        usage; exit 0 ;;
    --logs)
        check_docker; docker compose logs --tail 50; exit 0 ;;
    --status)
        check_docker; docker compose ps; exit 0 ;;
    --cleanup)
        check_docker; docker compose down
        echo "==> Stack removed. Cache volume and .env are preserved."
        exit 0 ;;
    --build-only)
        check_docker; docker compose build
        echo "==> Image built."
        exit 0 ;;
    ""|--no-test)
        ;;
    *)
        echo "!!! Unknown option: $1" >&2; usage >&2; exit 1 ;;
esac

check_docker

if [ ! -f .env ]; then
    ./deploy/bootstrap.sh
fi

docker compose up -d --build

if [ "${1:-}" != "--no-test" ]; then
    health_test
fi

echo
echo "==> Deployed. MCP endpoint: http://127.0.0.1:$(host_port)/mcp"
echo "    Logs:   ./deploy.sh --logs   (or: docker compose logs -f)"
echo "    Stop:   ./deploy.sh --cleanup"
