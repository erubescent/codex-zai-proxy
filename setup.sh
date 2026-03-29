#!/usr/bin/env bash
# setup.sh - Automated setup for codex-zai-proxy
# Works on Debian/Ubuntu (Docker) and Fedora/Bluefin (Podman)
#
# Usage:
#   export ZAI_API_KEY="your-key-here" && ./setup.sh   # bash
#   set -x ZAI_API_KEY "your-key-here"; ./setup.sh     # fish
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { printf "${BLUE}[INFO]${NC}  %s\n" "$*"; }
ok()   { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
fail() { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

# --- Detect container runtime ---
detect_runtime() {
    if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
        RUNTIME="docker"
        if docker compose version &>/dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
        elif command -v docker-compose &>/dev/null; then
            COMPOSE_CMD="docker-compose"
        fi
    elif command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
        RUNTIME="podman"
        if podman compose version &>/dev/null 2>&1; then
            COMPOSE_CMD="podman compose"
        elif command -v podman-compose &>/dev/null; then
            COMPOSE_CMD="podman-compose"
        fi
    fi
    RUNTIME="${RUNTIME:-}"
    COMPOSE_CMD="${COMPOSE_CMD:-}"
    [ -z "$RUNTIME" ] && fail "Neither Docker nor Podman found."
}

# --- Get API key ---
get_api_key() {
    # From environment
    [ -n "${ZAI_API_KEY:-}" ] && { API_KEY="$ZAI_API_KEY"; return 0; }
    # From .env file
    if [ -f ".env" ]; then
        local k; k=$(grep '^ZAI_API_KEY=' .env | head -1 | cut -d= -f2-)
        [ -n "$k" ] && { API_KEY="$k"; return 0; }
    fi
    # From fish (if available)
    if command -v fish &>/dev/null; then
        # shellcheck disable=SC2016
        local fv; fv=$(fish -c 'echo $ZAI_API_KEY' 2>/dev/null || true)
        [ -n "$fv" ] && { API_KEY="$fv"; return 0; }
    fi
    fail "ZAI_API_KEY not found. Export it or create .env with ZAI_API_KEY=your-key"
}

# --- Write .env ---
write_env() {
    printf 'ZAI_API_KEY=%s\n' "$API_KEY" > .env
    chmod 600 .env
    ok ".env written (mode 600)"
}

# --- Fix file permissions (root cause of PermissionError in container) ---
fix_permissions() {
    chmod -R a+rX proxy/
    ok "proxy/ permissions fixed (a+rX)"
}

# --- Build and start ---
start_container() {
    if [ -n "$COMPOSE_CMD" ]; then
        info "Building and starting with ${COMPOSE_CMD}..."
        $COMPOSE_CMD up -d --build
    elif [ "$RUNTIME" = "podman" ]; then
        info "Building with podman..."
        podman build -t codex-zai-proxy .
        podman rm -f codex-zai-proxy 2>/dev/null || true
        podman run -d \
            --name codex-zai-proxy \
            --env-file .env \
            --publish "127.0.0.1:4891:4891" \
            --restart unless-stopped \
            --security-opt no-new-privileges \
            --cap-drop ALL \
            --read-only \
            --tmpfs /tmp:rw,noexec,nosuid,size=64m \
            codex-zai-proxy
    else
        fail "Docker without compose is not supported. Install docker-compose-plugin."
    fi
}

# --- Wait for health ---
wait_for_health() {
    local attempts=0 max=30 url="http://127.0.0.1:4891/health"
    info "Waiting for proxy to start (up to ${max}s)..."
    while [ $attempts -lt $max ]; do
        if curl -sf "$url" >/dev/null 2>&1; then
            ok "Proxy healthy at ${url}"
            return 0
        fi
        sleep 1; attempts=$((attempts + 1))
    done
    warn "Proxy not healthy after ${max}s. Check logs:"
    if [ -n "$COMPOSE_CMD" ]; then
        warn "  ${COMPOSE_CMD} logs codex-zai-proxy"
    else
        warn "  podman logs codex-zai-proxy"
    fi
    warn "Continuing setup anyway..."
}

# --- Install Codex CLI if missing ---
install_codex() {
    if command -v codex &>/dev/null; then
        ok "Codex CLI found at $(command -v codex)"
        return 0
    fi
    info "Codex CLI not found. Installing..."
    if command -v npm &>/dev/null; then
        npm install -g @openai/codex 2>&1 && ok "Codex CLI installed" && return 0
    fi
    warn "Could not install Codex CLI automatically."
    warn "Install manually: npm install -g @openai/codex"
    return 0
}

# --- Configure Codex ---
configure_codex() {
    local config_dir="${HOME}/.codex"
    local config_file="${config_dir}/config.toml"
    mkdir -p "$config_dir"

    # Backup existing
    if [ -f "$config_file" ]; then
        local bak="${config_file}.bak.$(date +%Y%m%d%H%M%S)"
        cp "$config_file" "$bak"
        ok "Backed up config to ${bak}"
    fi

    # Check if already configured
    if [ -f "$config_file" ] && grep -q 'z_ai_proxy' "$config_file" 2>/dev/null; then
        ok "Codex provider z_ai_proxy already in config"
        return 0
    fi

    # Write config (preserving existing trust entries)
    local trust=""
    [ -f "$config_file" ] && trust=$(grep -E '^\[projects\.' "$config_file" 2>/dev/null || true)

    cat > "$config_file" <<'TOML'
profile = "glm_5_1_proxy"

[model_providers.z_ai_proxy]
name = "z.ai via Local Proxy"
base_url = "http://127.0.0.1:4891/v1"
env_key = "ZAI_API_KEY"
wire_api = "responses"

[profiles.glm_5_1_proxy]
model = "glm-5.1"
model_provider = "z_ai_proxy"
TOML

    if [ -n "$trust" ]; then
        printf '\n%s\n' "$trust" >> "$config_file"
    fi
    ok "Codex config written to ${config_file}"
}

# --- Persist API key to shell config ---
persist_key() {
    local rc=""
    case "${SHELL:-}" in
        */fish) rc="${HOME}/.config/fish/config.fish" ;;
        */zsh)  rc="${HOME}/.zshrc" ;;
        */bash) rc="${HOME}/.bashrc" ;;
    esac
    [ -z "$rc" ] && { warn "Unknown shell. Set ZAI_API_KEY manually."; return 0; }

    mkdir -p "$(dirname "$rc")"
    if grep -q 'ZAI_API_KEY' "$rc" 2>/dev/null; then
        ok "ZAI_API_KEY already in ${rc}"
    else
        if [ "${SHELL:-}" = "*/fish" ]; then
            printf '\n# Z.ai API Key (Codex CLI)\nset -gx ZAI_API_KEY "%s"\n' "$API_KEY" >> "$rc"
        else
            printf '\n# Z.ai API Key (Codex CLI)\nexport ZAI_API_KEY="%s"\n' "$API_KEY" >> "$rc"
        fi
        ok "Added ZAI_API_KEY to ${rc}"
    fi
    warn "Run: source ${rc}  (or restart shell)"
}

# --- Main ---
main() {
    echo; echo "codex-zai-proxy setup"; echo "====================="; echo

    detect_runtime
    ok "Runtime: ${RUNTIME}${COMPOSE_CMD:+ (compose)}"

    get_api_key
    ok "API key found (${#API_KEY} chars)"

    write_env
    fix_permissions
    start_container
    wait_for_health
    install_codex
    configure_codex
    persist_key

    echo
    printf "%s  Setup Complete%s\n" "${GREEN}" "${NC}"
    echo
    echo "  Proxy:     http://127.0.0.1:4891/health"
    echo "  Runtime:   ${RUNTIME}"
    if command -v codex &>/dev/null; then
        echo "  Codex:     $(command -v codex)"
    fi
    echo
    echo "  Run: codex"
    echo
}

main "$@"
