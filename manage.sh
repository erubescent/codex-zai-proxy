#!/usr/bin/env bash
# manage.sh - Manage the Codex-Z.ai Proxy container
set -euo pipefail

IMAGE_NAME="codex-zai-proxy"
CONTAINER_NAME="codex-zai-proxy"
PROXY_PORT=4891
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${PROJECT_DIR}/.env"

_usage() {
    cat <<EOF
Usage: $(basename "$0") {build|start|stop|restart|logs|status|shell|rebuild}

Commands:
  build    Build the container image
  start    Start the proxy container
  stop     Stop and remove the proxy container
  restart  Stop then start
  logs     Follow container logs
  status   Show container status
  shell    Open a shell in the container
  rebuild  Rebuild and restart
  test     Run a quick health check
EOF
}

_build() {
    echo "Building ${IMAGE_NAME}..."
    podman build -t "${IMAGE_NAME}" "${PROJECT_DIR}"
    echo "Build complete."
}

_start() {
    if podman ps -q --filter "name=${CONTAINER_NAME}" | grep -q .; then
        echo "Container ${CONTAINER_NAME} is already running."
        return 0
    fi

    if [ ! -f "${ENV_FILE}" ]; then
        echo "ERROR: ${ENV_FILE} not found. Copy .env.example to .env and set ZAI_API_KEY."
        exit 1
    fi

    # Check if key is set
    if grep -q '^ZAI_API_KEY=$' "${ENV_FILE}" 2>/dev/null || grep -q '^ZAI_API_KEY=""' "${ENV_FILE}" 2>/dev/null; then
        echo "WARNING: ZAI_API_KEY appears empty in .env"
    fi

    echo "Starting ${CONTAINER_NAME} on 127.0.0.1:${PROXY_PORT}..."
    podman run -d \
        --name "${CONTAINER_NAME}" \
        --env-file "${ENV_FILE}" \
        --publish "127.0.0.1:${PROXY_PORT}:${PROXY_PORT}" \
        --restart unless-stopped \
        --security-opt no-new-privileges \
        --cap-drop ALL \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        "${IMAGE_NAME}"

    echo "Started. Health: http://127.0.0.1:${PROXY_PORT}/health"
}

_stop() {
    if podman ps -aq --filter "name=${CONTAINER_NAME}" | grep -q .; then
        echo "Stopping ${CONTAINER_NAME}..."
        podman rm -f "${CONTAINER_NAME}" 2>/dev/null || true
        echo "Stopped."
    else
        echo "Container ${CONTAINER_NAME} is not running."
    fi
}

_logs() {
    podman logs -f "${CONTAINER_NAME}" 2>/dev/null || echo "No logs (container not found)"
}

_status() {
    podman ps -a --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null
}

_shell() {
    podman exec -it "${CONTAINER_NAME}" /bin/bash 2>/dev/null || echo "Container not running"
}

_test() {
    echo "Testing proxy health..."
    curl -s "http://127.0.0.1:${PROXY_PORT}/health" | python3 -m json.tool 2>/dev/null || \
        echo "FAILED: Proxy not responding on 127.0.0.1:${PROXY_PORT}"
}

case "${1:-}" in
    build)   _build ;;
    start)   _start ;;
    stop)    _stop ;;
    restart) _stop; _start ;;
    logs)    _logs ;;
    status)  _status ;;
    shell)   _shell ;;
    rebuild) _stop; _build; _start ;;
    test)    _test ;;
    *)       _usage ;;
esac
