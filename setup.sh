#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# setup.sh - Automated setup for codex-zai-proxy
#
# Detects Docker or Podman, builds the proxy container, starts it,
# and configures Codex CLI to use it. Works on:
#   - Fedora / Bluefin / Silverblue (Podman)
#   - Debian / Ubuntu (Docker)
#   - Any Linux with Docker or Podman installed
#
# Usage:
#   export ZAI_API_KEY="your-key-here"
#   ./setup.sh
#
# Or with fish:
#   set -x ZAI_API_KEY "your-key-here"
#   ./setup.sh
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
# Colors and output helpers
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

info()    { printf "${BLUE}[INFO]${NC}  %s\n" "$*"; }
ok()      { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn()    { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
fail()    { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Detect runtime: Docker or Podman
# ---------------------------------------------------------------------------
detect_runtime() {
    if command -v docker &>/dev/null; then
        # Check if docker is actually functional
        if docker info &>/dev/null 2>&1; then
            RUNTIME="docker"
            COMPOSE_CMD=""
            # Detect compose (v2 plugin or standalone)
            if docker compose version &>/dev/null 2>&1; then
                COMPOSE_CMD="docker compose"
            elif command -v docker-compose &>/dev/null; then
                COMPOSE_CMD="docker-compose"
            fi
            return 0
        fi
    fi
    if command -v podman &>/dev/null; then
        if podman info &>/dev/null 2>&1; then
            RUNTIME="podman"
            COMPOSE_CMD=""
            if podman compose version &>/dev/null 2>&1; then
                COMPOSE_CMD="podman compose"
            elif command -v podman-compose &>/dev/null; then
                COMPOSE_CMD="podman-compose"
            fi
            return 0
        fi
    fi
    fail "Neither Docker nor Podman found. Install one and try again."
}

# ---------------------------------------------------------------------------
# Detect shell rc file
# ---------------------------------------------------------------------------
detect_shell_rc() {
    local shell_name=""
    case "${SHELL:-}" in
        */fish) shell_name="fish" ;;
        */zsh)  shell_name="zsh" ;;
        */bash) shell_name="bash" ;;
        *)      shell_name="unknown" ;;
    esac

    case "$shell_name" in
        fish) SHELL_RC="${HOME}/.config/fish/config.fish" ;;
        zsh)  SHELL_RC="${HOME}/.zshrc" ;;
        bash) SHELL_RC="${HOME}/.bashrc" ;;
        *)    SHELL_RC="" ;;
    esac
    SHELL_NAME="$shell_name"
}

# ---------------------------------------------------------------------------
# Detect Codex CLI
# ---------------------------------------------------------------------------
detect_codex() {
    if command -v codex &>/dev/null; then
        CODEX_BIN="$(command -v codex)"
        CODEX_CONFIG="${HOME}/.codex/config.toml"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# Get API key from environment
# ---------------------------------------------------------------------------
get_api_key() {
    # Check environment variable
    if [ -n "${ZAI_API_KEY:-}" ]; then
        API_KEY="$ZAI_API_KEY"
        return 0
    fi

    # Check existing .env file
    if [ -f ".env" ]; then
        local key
        key=$(grep '^ZAI_API_KEY=' .env | head -1 | cut -d= -f2-)
        if [ -n "$key" ]; then
            API_KEY="$key"
            return 0
        fi
    fi

    # Check fish universal variables (if fish is available)
    if command -v fish &>/dev/null; then
        local fish_val
        # shellcheck disable=SC2016  # intentional: fish expands $ZAI_API_KEY, not bash
        fish_val=$(fish -c 'echo $ZAI_API_KEY' 2>/dev/null || true)
        if [ -n "$fish_val" ]; then
            API_KEY="$fish_val"
            return 0
        fi
    fi

    fail "ZAI_API_KEY not found. Set it before running:
  bash: export ZAI_API_KEY=\"your-key\"
  fish:  set -x ZAI_API_KEY \"your-key\"
  Or create a .env file with ZAI_API_KEY=your-key"
}

# ---------------------------------------------------------------------------
# Write .env file
# ---------------------------------------------------------------------------
write_env() {
    cat > .env <<EOF
ZAI_API_KEY=${API_KEY}
EOF
    chmod 600 .env
    ok ".env written (mode 600)"
}

# ---------------------------------------------------------------------------
# Build and start with Docker Compose
# ---------------------------------------------------------------------------
start_compose() {
    info "Starting with ${COMPOSE_CMD}..."
    $COMPOSE_CMD up -d --build
    ok "Container started via compose"
}

# ---------------------------------------------------------------------------
# Build and start with Podman directly (for systems without compose)
# ---------------------------------------------------------------------------
start_podman_direct() {
    local image_name="codex-zai-proxy"
    local container_name="codex-zai-proxy"

    info "Building image..."
    podman build -t "$image_name" .

    # Remove existing container if any
    podman rm -f "$container_name" 2>/dev/null || true

    info "Starting container..."
    podman run -d \
        --name "$container_name" \
        --env-file .env \
        --publish "127.0.0.1:4891:4891" \
        --restart unless-stopped \
        --security-opt no-new-privileges \
        --cap-drop ALL \
        --read-only \
        --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        "$image_name"

    ok "Container started via podman run"
}

# ---------------------------------------------------------------------------
# Configure Codex CLI
# ---------------------------------------------------------------------------
configure_codex() {
    if ! detect_codex; then
        warn "Codex CLI not found. Skipping Codex config."
        warn "Install with: npm install -g @openai/codex"
        return 0
    fi

    info "Codex CLI found at ${CODEX_BIN}"

    # Backup existing config
    if [ -f "$CODEX_CONFIG" ]; then
        local backup
        backup="${CODEX_CONFIG}.bak.$(date +%Y%m%d%H%M%S)"
        cp "$CODEX_CONFIG" "$backup"
        ok "Backed up existing config to ${backup}"
    fi

    # Check if our provider already exists
    if [ -f "$CODEX_CONFIG" ] && grep -q 'z_ai_proxy' "$CODEX_CONFIG" 2>/dev/null; then
        ok "Codex provider z_ai_proxy already configured"
        return 0
    fi

    # Build new config
    # Preserve existing content if any, add our provider
    local config_dir
    config_dir="$(dirname "$CODEX_CONFIG")"
    mkdir -p "$config_dir"

    # Read existing trust entries to preserve them
    local trust_entries=""
    if [ -f "$CODEX_CONFIG" ]; then
        # Extract existing project trust entries
        trust_entries=$(grep -A1 '^\[projects\.' "$CODEX_CONFIG" 2>/dev/null || true)
    fi

    cat > "$CODEX_CONFIG" <<'CODEX_EOF'
profile = "glm_5_1_proxy"

[model_providers.z_ai_proxy]
name = "z.ai via Local Proxy"
base_url = "http://127.0.0.1:4891/v1"
env_key = "ZAI_API_KEY"
wire_api = "responses"

[profiles.glm_5_1_proxy]
model = "glm-5.1"
model_provider = "z_ai_proxy"
CODEX_EOF

    # Re-add trust entries if they existed
    if [ -n "$trust_entries" ]; then
        echo "" >> "$CODEX_CONFIG"
        echo "$trust_entries" >> "$CODEX_CONFIG"
    fi

    ok "Codex config written to ${CODEX_CONFIG}"
}

# ---------------------------------------------------------------------------
# Ensure API key is available in shell environment
# ---------------------------------------------------------------------------
ensure_env_var() {
    detect_shell_rc

    # Check if already exported in current shell
    if [ -n "${ZAI_API_KEY:-}" ]; then
        return 0
    fi

    # Fish: check config.fish
    if [ "$SHELL_NAME" = "fish" ]; then
        local fish_env_file="${HOME}/.config/fish/config.fish"
        mkdir -p "$(dirname "$fish_env_file")"
        if ! grep -q 'ZAI_API_KEY' "$fish_env_file" 2>/dev/null; then
            {
                echo ""
                echo "# Z.ai API Key (for Codex CLI)"
                echo "set -gx ZAI_API_KEY \"${API_KEY}\""
            } >> "$fish_env_file"
            ok "Added ZAI_API_KEY to ${fish_env_file}"
        else
            ok "ZAI_API_KEY already in ${fish_env_file}"
        fi
        warn "Run 'source ~/.config/fish/config.fish' or restart your shell for the variable to take effect"
        return 0
    fi

    # Bash/Zsh: check rc file
    if [ -n "$SHELL_RC" ]; then
        if ! grep -q 'ZAI_API_KEY' "$SHELL_RC" 2>/dev/null; then
            {
                echo ""
                echo "# Z.ai API Key (for Codex CLI)"
                echo "export ZAI_API_KEY=\"${API_KEY}\""
            } >> "$SHELL_RC"
            ok "Added ZAI_API_KEY to ${SHELL_RC}"
        else
            ok "ZAI_API_KEY already in ${SHELL_RC}"
        fi
        warn "Run 'source ${SHELL_RC}' or restart your shell for the variable to take effect"
        return 0
    fi

    warn "Could not detect shell rc file. Set ZAI_API_KEY manually before using Codex."
}

# ---------------------------------------------------------------------------
# Wait for health check
# ---------------------------------------------------------------------------
wait_for_health() {
    local max_attempts=15
    local attempt=1
    local url="http://127.0.0.1:4891/health"

    info "Waiting for proxy to become healthy..."
    while [ $attempt -le $max_attempts ]; do
        if curl -sf "$url" >/dev/null 2>&1; then
            ok "Proxy is healthy at ${url}"
            return 0
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    warn "Proxy did not become healthy within ${max_attempts}s"
    warn "Check logs: ${COMPOSE_CMD:-podman} logs codex-zai-proxy"
    return 1
}

# ---------------------------------------------------------------------------
# Print summary
# ---------------------------------------------------------------------------
print_summary() {
    echo ""
    echo "=========================================="
    printf "%s  Setup Complete%s\n" "${GREEN}" "${NC}"
    echo "=========================================="
    echo ""
    echo "  Runtime:   ${RUNTIME}"
    echo "  Compose:   ${COMPOSE_CMD:-none (using direct run)}"
    echo "  Proxy:     http://127.0.0.1:4891"
    echo "  Health:    http://127.0.0.1:4891/health"
    echo "  Shell:     ${SHELL_NAME:-unknown}"
    if detect_codex; then
        echo "  Codex:     ${CODEX_BIN}"
        echo "  Config:    ${CODEX_CONFIG}"
    fi
    echo ""
    echo "  Management:"
    if [ -n "$COMPOSE_CMD" ]; then
        echo "    ${COMPOSE_CMD} logs -f codex-zai-proxy"
        echo "    ${COMPOSE_CMD} restart"
        echo "    ${COMPOSE_CMD} down"
    else
        echo "    podman logs -f codex-zai-proxy"
        echo "    podman restart codex-zai-proxy"
        echo "    podman rm -f codex-zai-proxy"
    fi
    echo ""
    echo "  Start Codex:"
    echo "    codex"
    echo ""
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
main() {
    echo ""
    echo "codex-zai-proxy setup"
    echo "====================="
    echo ""

    # 1. Detect environment
    info "Detecting container runtime..."
    detect_runtime
    ok "Using ${RUNTIME}${COMPOSE_CMD:+ (compose: ${COMPOSE_CMD})}"

    info "Detecting shell..."
    detect_shell_rc
    ok "Shell: ${SHELL_NAME:-unknown} (${SHELL_RC:-no rc file})"

    # 2. Get API key
    info "Looking for ZAI_API_KEY..."
    get_api_key
    ok "API key found (${#API_KEY} chars)"

    # 3. Write .env
    write_env

    # 4. Build and start
    if [ -n "$COMPOSE_CMD" ]; then
        start_compose
    else
        if [ "$RUNTIME" = "podman" ]; then
            start_podman_direct
        else
            fail "Docker without compose is not supported. Install docker-compose-plugin."
        fi
    fi

    # 5. Wait for health
    wait_for_health

    # 6. Configure Codex
    info "Configuring Codex CLI..."
    configure_codex

    # 7. Ensure env var is persisted
    ensure_env_var

    # 8. Summary
    print_summary
}

main "$@"
