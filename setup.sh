#!/usr/bin/env bash
# setup.sh - Automated setup for codex-zai-proxy
# Works on Debian/Ubuntu (Docker) and Fedora/Bluefin (Podman)
#
# Usage:
#   export ZAI_API_KEY="your-key-here" && ./setup.sh
#   set -x ZAI_API_KEY "your-key-here"; ./setup.sh  # fish
set -uo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info() { printf "${BLUE}[INFO]${NC}  %s\n" "$*"; }
ok()   { printf "${GREEN}[OK]${NC}    %s\n" "$*"; }
warn() { printf "${YELLOW}[WARN]${NC}  %s\n" "$*"; }
die()  { printf "${RED}[FAIL]${NC}  %s\n" "$*" >&2; exit 1; }

cd "$(dirname "$0")" || exit 1

# --- Detect runtime ---
info "Detecting container runtime..."
RUNTIME="" COMPOSE_CMD=""
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    RUNTIME=docker
    docker compose version &>/dev/null 2>&1 && COMPOSE_CMD="docker compose"
    [ -z "$COMPOSE_CMD" ] && command -v docker-compose &>/dev/null && COMPOSE_CMD="docker-compose"
elif command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then
    RUNTIME=podman
    podman compose version &>/dev/null 2>&1 && COMPOSE_CMD="podman compose"
    [ -z "$COMPOSE_CMD" ] && command -v podman-compose &>/dev/null && COMPOSE_CMD="podman-compose"
fi
[ -z "$RUNTIME" ] && die "Neither Docker nor Podman found."
ok "Runtime: ${RUNTIME}${COMPOSE_CMD:+ (compose)}"

# --- Get API key ---
info "Looking for ZAI_API_KEY..."
API_KEY=""
[ -n "${ZAI_API_KEY:-}" ] && API_KEY="$ZAI_API_KEY"
[ -z "$API_KEY" ] && [ -f .env ] && API_KEY=$(grep '^ZAI_API_KEY=' .env | head -1 | cut -d= -f2-)
[ -z "$API_KEY" ] && command -v fish &>/dev/null && API_KEY=$(fish -c 'echo $ZAI_API_KEY' 2>/dev/null || true)
[ -z "$API_KEY" ] && die "ZAI_API_KEY not found. Export it or create .env"
ok "API key found (${#API_KEY} chars)"

# --- Write .env ---
printf 'ZAI_API_KEY=%s\n' "$API_KEY" > .env
chmod 600 .env
ok ".env written (mode 600)"

# --- Fix file permissions for container ---
chmod -R a+rX proxy/
ok "proxy/ permissions fixed"

# --- Build and start ---
if [ -n "$COMPOSE_CMD" ]; then
    info "Building and starting with ${COMPOSE_CMD}..."
    $COMPOSE_CMD up -d --build || die "Failed to start container"
elif [ "$RUNTIME" = "podman" ]; then
    info "Building with podman..."
    podman build -t codex-zai-proxy . || die "Build failed"
    podman rm -f codex-zai-proxy 2>/dev/null
    podman run -d --name codex-zai-proxy --env-file .env \
        --publish 127.0.0.1:4891:4891 --restart unless-stopped \
        --security-opt no-new-privileges --cap-drop ALL \
        --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
        codex-zai-proxy || die "Failed to start container"
else
    die "Docker without compose. Install docker-compose-plugin."
fi
ok "Container started"

# --- Wait for health ---
info "Waiting for proxy (up to 30s)..."
HEALTHY=false
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:4891/health >/dev/null 2>&1; then
        ok "Proxy healthy"
        HEALTHY=true
        break
    fi
    sleep 1
done
[ "$HEALTHY" = "false" ] && warn "Proxy not healthy. Check: ${COMPOSE_CMD:-podman} logs codex-zai-proxy"

# --- Install Codex CLI if missing ---
if ! command -v codex &>/dev/null; then
    info "Installing Codex CLI..."
    if command -v npm &>/dev/null; then
        npm install -g @openai/codex && ok "Codex CLI installed"
    else
        warn "npm not found. Install Codex manually: npm install -g @openai/codex"
    fi
else
    ok "Codex CLI found at $(command -v codex)"
fi

# --- Configure Codex ---
if command -v codex &>/dev/null; then
    info "Configuring Codex CLI..."
    CFG="${HOME}/.codex/config.toml"
    mkdir -p "$(dirname "$CFG")"
    [ -f "$CFG" ] && cp "$CFG" "${CFG}.bak.$(date +%Y%m%d%H%M%S)"
    if [ -f "$CFG" ] && grep -q 'z_ai_proxy' "$CFG" 2>/dev/null; then
        ok "Codex already configured"
    else
        cat > "$CFG" <<'TOML'
profile = "glm_proxy"

[model_providers.z_ai_proxy]
name = "z.ai via Local Proxy"
base_url = "http://127.0.0.1:4891/v1"
env_key = "ZAI_API_KEY"
wire_api = "responses"

[profiles.glm_proxy]
model = "glm-5.1"
model_provider = "z_ai_proxy"
TOML
        ok "Codex config written"
    fi
fi

# --- Persist key to shell rc ---
RC=""
case "${SHELL:-}" in */fish) RC="${HOME}/.config/fish/config.fish";; */zsh) RC="${HOME}/.zshrc";; */bash) RC="${HOME}/.bashrc";; esac
if [ -n "$RC" ] && ! grep -q 'ZAI_API_KEY' "$RC" 2>/dev/null; then
    mkdir -p "$(dirname "$RC")"
    printf '\n# Z.ai API Key (Codex CLI)\nexport ZAI_API_KEY="%s"\n' "$API_KEY" >> "$RC"
    ok "Key added to ${RC}"
fi

echo
printf "%s  Setup Complete%s\n" "${GREEN}" "${NC}"
echo "  Proxy:  http://127.0.0.1:4891/health"
echo "  Manage: ${COMPOSE_CMD:-podman} logs/restart/down codex-zai-proxy"
echo "  Start:  codex"
echo
