# codex-zai-proxy

A localhost-only reverse proxy that bridges OpenAI Codex CLI with Z.ai's GLM Coding Plan API. Translates the Responses API wire format (which Codex CLI requires) into Chat Completions API requests (which Z.ai accepts), handling the full request/response lifecycle including streaming SSE and function/tool call translation.

## Why

Codex CLI (`@openai/codex`) removed support for `wire_api = "chat"` and now requires `wire_api = "responses"`. Z.ai's coding endpoint speaks the Chat Completions protocol. This proxy sits between them, translating in both directions so Codex works transparently with a Z.ai subscription.

## Architecture

```
 Codex CLI                    Localhost Proxy                   Z.ai API
 ─────────                    ───────────────                   ────────
 POST /v1/responses    ──►    Request translation    ──►    POST /chat/completions
 (Responses API format)       • instructions → system msg     (Chat Completions format)
                              • input items → messages
                              • tool format conversion
                              • role normalization

 SSE stream (Responses) ◄──   Response translation   ◄──    SSE stream (Chat Completions)
                              • chat chunks → response events
                              • tool_call deltas tracked
                              • proper SSE event sequencing
```

## Requirements

- [Podman](https://podman.io/) (rootless)
- [Codex CLI](https://github.com/openai/codex) (`npm install -g @openai/codex`)
- Z.ai GLM Coding Plan subscription with API key

## Quick Start

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/codex-zai-proxy.git
cd codex-zai-proxy

# Configure your API key
cp .env.example .env
# Edit .env and set ZAI_API_KEY=your-key-here
chmod 600 .env

# Build and start
podman build -t codex-zai-proxy .
podman run -d \
  --name codex-zai-proxy \
  --env-file .env \
  --publish 127.0.0.1:4891:4891 \
  --restart unless-stopped \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  codex-zai-proxy

# Verify
curl http://127.0.0.1:4891/health
```

## Codex Configuration

Add this to `~/.codex/config.toml`:

```toml
profile = "glm_5_1_proxy"

[model_providers.z_ai_proxy]
name = "z.ai via Local Proxy"
base_url = "http://127.0.0.1:4891/v1"
env_key = "ZAI_API_KEY"
wire_api = "responses"

[profiles.glm_5_1_proxy]
model = "glm-5.1"
model_provider = "z_ai_proxy"
```

Make sure `ZAI_API_KEY` is set in your shell environment, then run:

```bash
codex
```

## Translation Details

### Request (Responses → Chat Completions)

| Responses API | Chat Completions |
|---|---|
| `instructions` field | `system` role message |
| `input[]` with `type: "message"` | `messages[]` with `role` + `content` |
| `input[]` with `type: "function_call"` | Assistant message with `tool_calls[]` |
| `input[]` with `type: "function_call_output"` | `tool` role message |
| `input[]` with `type: "local_shell_call"` | Mapped to `function` tool_call |
| `tools[]` with `{type, name, parameters}` | `tools[]` with `{type: "function", function: {name, parameters}}` |
| `role: "developer"` | `role: "system"` |

### Response (Chat Completions SSE → Responses API SSE)

The proxy consumes the upstream Chat Completions stream and emits properly sequenced Responses API events:

1. `response.created` — emitted immediately
2. `response.output_item.added` — when text or tool output begins
3. `response.output_text.delta` — each text chunk from upstream
4. `response.output_item.done` — completed text message or function call
5. `response.completed` — final event with usage stats

Tool call arguments are accumulated across multiple deltas and emitted as a complete `function_call` output item.

## Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check, returns upstream URL |
| `POST` | `/v1/responses` | Main proxy endpoint (Codex hits this) |
| `GET` | `/v1/models` | Minimal models endpoint for compatibility |

## Persistent Service (systemd)

For auto-start on login, use the included Quadlet:

```bash
# Copy Quadlet file
mkdir -p ~/.config/containers/systemd
cp codex-zai-proxy.container ~/.config/containers/systemd/

# Generate and enable
/usr/libexec/podman/quadlet --user ~/.config/systemd/user/generated
cp ~/.config/systemd/user/generated/codex-zai-proxy.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now codex-zai-proxy
```

Management:

```bash
systemctl --user start codex-zai-proxy
systemctl --user stop codex-zai-proxy
systemctl --user restart codex-zai-proxy
systemctl --user status codex-zai-proxy
journalctl --user -u codex-zai-proxy -f
```

## Management Script

```bash
./manage.sh build     # Build the container image
./manage.sh start     # Start container
./manage.sh stop      # Stop and remove container
./manage.sh restart   # Stop then start
./manage.sh rebuild   # Rebuild image and restart
./manage.sh logs      # Follow container logs
./manage.sh status    # Show container status
./manage.sh test      # Health check
```

## Security

- Binds to **127.0.0.1 only** — Podman `--publish 127.0.0.1:4891:4891` enforces localhost-only access
- **Non-root** process inside container
- **Read-only** container filesystem with tmpfs for `/tmp`
- **All Linux capabilities dropped** (`--cap-drop ALL`)
- **No new privileges** security option set
- API key loaded from `.env` file (mode 600) at runtime — never baked into the image
- Upstream errors are sanitized before returning to the client

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ZAI_API_KEY` | (required) | Your Z.ai API key |
| `ZAI_BASE_URL` | `https://api.z.ai/api/coding/paas/v4` | Z.ai API base URL |
| `PROXY_PORT` | `4891` | Port to listen on |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

## Limitations

- **Reasoning summaries** — The `reasoning` field is stripped from requests since GLM models don't support it
- **Built-in tools** — OpenAI-specific tools (`web_search`, `file_search`, `code_interpreter`) are not forwarded
- **`previous_response_id`** — Not supported; full conversation history is sent each turn (same as Codex's HTTP transport behavior)
- **Model-specific features** — Any feature requiring OpenAI server-side infrastructure won't work

## Rebuilding After Changes

```bash
podman build -t codex-zai-proxy .
systemctl --user restart codex-zai-proxy
```

## License

MIT
