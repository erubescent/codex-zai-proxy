# Codex-Z.ai Proxy - Context

## Model Switching (Single Source of Truth)
Change ONLY `~/.codex/config.toml`:
```toml
[profiles.glm_proxy]
model = "glm-5"    # or "glm-5.1", "glm-4.7"
```
No proxy restart needed. Start a new Codex session.

## Config File (~/.codex/config.toml)
```toml
profile = "glm_proxy"

[model_providers.z_ai_proxy]
name = "z.ai via Local Proxy"
base_url = "http://127.0.0.1:4891/v1"
env_key = "Z_AI_API_KEY"
wire_api = "responses"

[profiles.glm_proxy]
model = "glm-5"
model_provider = "z_ai_proxy"
```

## Env Vars
- `ZAI_API_KEY` — Z.ai API key (required)
- `ZAI_BASE_URL` — upstream API URL (default: `https://api.z.ai/api/coding/paas/v4`)
- `PROXY_PORT` — local port (default: `4891`)
- `LOG_LEVEL` — logging level (default: `INFO`)

## Project Location
`/var/home/preston/podman/codex-zai-proxy/`

## Repo
https://github.com/erubescent/codex-zai-proxy

## Proxy Service
- Systemd user service: `systemctl --user restart codex-zai-proxy`
- Container name: `codex-zai-proxy`
- Port: `127.0.0.1:4891`

## Git User
erubescent <hello@noceurmedia.com>
