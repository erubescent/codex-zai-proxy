"""
Codex-to-Z.ai Responses API Proxy

Translates OpenAI Responses API requests (used by Codex CLI) into
OpenAI Chat Completions API requests (used by Z.ai), and translates
the streaming SSE responses back.

Codex CLI always uses streaming, so this proxy is streaming-first.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections import deque
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ZAI_BASE_URL = os.getenv(
    "ZAI_BASE_URL",
    "https://api.z.ai/api/coding/paas/v4",
).rstrip("/")

ZAI_API_KEY = os.getenv("ZAI_API_KEY", "")
PROXY_PORT = int(os.getenv("PROXY_PORT", "4891"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Known Z.ai models for /v1/models endpoint
ZAI_MODELS = ["glm-5", "glm-5.1", "glm-4.7"]

# Loop detection: max identical consecutive chunks before breaking
LOOP_REPEAT_LIMIT = 5

# Rate limiting: max requests per minute to Z.ai
ZAI_RPM = int(os.getenv("ZAI_RPM", "50"))
RETRY_MAX_ATTEMPTS = 3
import asyncio

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("codex-zai-proxy")

# ---------------------------------------------------------------------------
# Rate Limiter (token bucket)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple sliding-window rate limiter for upstream API calls."""

    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        now = time.time()
        # Prune timestamps older than 60s
        self._timestamps = [t for t in self._timestamps if now - t < 60]
        if len(self._timestamps) >= self.max_rpm:
            wait = 60.0 - (now - self._timestamps[0]) + 0.1
            log.warning("Rate limiter: waiting %.1fs (at %d/%d RPM)", wait, len(self._timestamps), self.max_rpm)
            await asyncio.sleep(wait)
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60]
        self._timestamps.append(time.time())


_rate_limiter = _RateLimiter(ZAI_RPM)

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Codex-Z.ai Proxy", version="1.1.0")

# Shared httpx client (connection pooling)
_http_client: httpx.AsyncClient | None = None


@app.on_event("startup")
async def startup():
    global _http_client
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=30.0),
        follow_redirects=True,
    )


@app.on_event("shutdown")
async def shutdown():
    if _http_client:
        await _http_client.aclose()


# ===================================================================
# Helpers
# ===================================================================

def _resp_id() -> str:
    return f"resp_{uuid.uuid4().hex[:24]}"


def _msg_id() -> str:
    return f"msg_{uuid.uuid4().hex[:24]}"


def _call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def _sse(event_type: str, data: dict[str, Any]) -> str:
    """Format one SSE event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _now() -> int:
    return int(time.time())


def _validate_and_fix_json(args_str: str, tool_name: str) -> str:
    """Validate tool call arguments are valid JSON, attempt fix if not."""
    try:
        json.loads(args_str)
        return args_str
    except json.JSONDecodeError as e:
        log.warning("Invalid tool call arguments for %s: %s, attempting fix", tool_name, e)
        fixed = args_str.rstrip()
        open_braces = fixed.count('{') - fixed.count('}')
        open_brackets = fixed.count('[') - fixed.count(']')
        fixed += '}' * max(0, open_braces) + ']' * max(0, open_brackets)
        try:
            json.loads(fixed)
            log.info("Fixed arguments for %s: added %d braces, %d brackets",
                     tool_name, max(0, open_braces), max(0, open_brackets))
            return fixed
        except json.JSONDecodeError:
            log.error("Could not fix arguments for %s, returning empty object", tool_name)
            return "{}"


# ===================================================================
# Request translation: Responses API -> Chat Completions
# ===================================================================

def _extract_text(content: Any) -> str:
    """Extract plain text from Responses API content (string or array of content parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                ptype = part.get("type", "")
                if ptype in ("input_text", "output_text", "text"):
                    parts.append(part.get("text", ""))
                elif "text" in part:
                    parts.append(part["text"])
        return "\n".join(parts)
    return str(content) if content else ""


def _translate_tools(tools: list[dict]) -> list[dict]:
    """Translate Responses API tools to Chat Completions tools format.

    Responses API format:
      { "type": "function", "name": "shell", "description": "...", "parameters": {...} }
    Chat Completions format:
      { "type": "function", "function": { "name": "shell", "description": "...", "parameters": {...} } }
    """
    chat_tools = []
    for tool in tools:
        ttype = tool.get("type", "function")
        if ttype == "function":
            if "function" in tool:
                chat_tools.append(tool)
            else:
                fn_obj = {}
                if "name" in tool:
                    fn_obj["name"] = tool["name"]
                if "description" in tool:
                    fn_obj["description"] = tool["description"]
                if "parameters" in tool:
                    fn_obj["parameters"] = tool["parameters"]
                if "strict" in tool:
                    fn_obj["strict"] = tool["strict"]
                chat_tools.append({"type": "function", "function": fn_obj})
    return chat_tools


def translate_request(body: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Responses API request body into a Chat Completions API request body.

    KEY FIX: Consolidates ALL system-level content (instructions + developer role
    messages) into a single system message at position 0. This prevents GLM models
    from getting confused by multiple system messages scattered mid-conversation,
    which was the primary cause of infinite planning loops.
    """
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []

    # 1. Collect system prompt from "instructions"
    instructions = body.get("instructions")
    if instructions:
        system_parts.append(instructions)

    # 2. Walk input items and build messages
    last_assistant_idx: int | None = None

    def _ensure_assistant() -> int:
        """Ensure the last message is an assistant message; create one if needed."""
        nonlocal last_assistant_idx
        if last_assistant_idx is not None and messages[last_assistant_idx]["role"] == "assistant":
            return last_assistant_idx
        msg: dict[str, Any] = {"role": "assistant", "content": ""}
        messages.append(msg)
        last_assistant_idx = len(messages) - 1
        return last_assistant_idx

    for item in body.get("input", []):
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            last_assistant_idx = None
            continue

        itype = item.get("type", "message")

        if itype == "message":
            role = item.get("role", "user")
            content = _extract_text(item.get("content"))

            if role == "developer":
                # FIX: Collect developer directives into system_parts instead
                # of creating mid-conversation system messages that confuse GLM
                if content:
                    system_parts.append(content)
                continue

            messages.append({"role": role, "content": content})
            if role == "assistant":
                last_assistant_idx = len(messages) - 1
            elif role == "system":
                pass
            else:
                last_assistant_idx = None

        elif itype == "function_call":
            idx = _ensure_assistant()
            tc = {
                "id": item.get("call_id", _call_id()),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
            }
            if "tool_calls" not in messages[idx]:
                messages[idx]["tool_calls"] = []
            messages[idx]["tool_calls"].append(tc)

        elif itype in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output", "")
            if isinstance(output, list):
                output = _extract_text(output)
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": str(output),
            })
            last_assistant_idx = None

        elif itype in ("local_shell_call", "custom_tool_call", "tool_search_call"):
            action = item.get("action", {})
            call_id = item.get("call_id", _call_id())

            name = action.get("type", itype)
            if itype == "local_shell_call":
                name = "shell"
            elif itype == "custom_tool_call":
                name = item.get("name", "custom_tool")

            args = json.dumps(action) if action else "{}"

            idx = _ensure_assistant()
            tc = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args},
            }
            if "tool_calls" not in messages[idx]:
                messages[idx]["tool_calls"] = []
            messages[idx]["tool_calls"].append(tc)

        elif itype == "reasoning":
            # FIX: Preserve reasoning context so GLM doesn't re-analyze
            reasoning_text = _extract_text(item.get("summary", item.get("content", "")))
            if reasoning_text:
                log.debug("Dropping reasoning item (%d chars): %s", len(reasoning_text), reasoning_text[:200])
                summary = reasoning_text[:500]
                messages.append({
                    "role": "assistant",
                    "content": f"[Previous reasoning: {summary}]",
                })
                last_assistant_idx = len(messages) - 1
            else:
                log.debug("Dropping empty reasoning item")

        else:
            log.debug("Skipping unknown input item type: %s", itype)

    # FIX: Consolidate all system content into ONE system message at position 0
    if system_parts:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_parts)})

    # 3. Build the chat completions request
    chat_req: dict[str, Any] = {
        "model": body.get("model", "glm-5.1"),
        "messages": messages,
        "stream": True,
    }

    # Copy optional parameters
    for key in ("temperature", "top_p", "max_tokens", "max_completion_tokens", "n", "stop"):
        if key in body:
            chat_req[key] = body[key]

    # Map Responses API max_output_tokens -> Chat Completions max_completion_tokens
    if "max_output_tokens" in body and "max_completion_tokens" not in body:
        chat_req["max_completion_tokens"] = body["max_output_tokens"]

    # Translate tools
    tools = body.get("tools")
    if tools:
        chat_tools = _translate_tools(tools)
        if chat_tools:
            chat_req["tools"] = chat_tools
            chat_req["tool_choice"] = body.get("tool_choice", "auto")
            if body.get("parallel_tool_calls") is not None:
                chat_req["parallel_tool_calls"] = body["parallel_tool_calls"]

    log.info("Translated request: %d messages, model=%s, system_parts=%d",
             len(messages), chat_req["model"], len(system_parts))
    log.debug("Translated messages: %s", json.dumps(messages[:3], default=str)[:2000])
    if chat_req.get("tools"):
        log.debug("Tools: %s", [t.get("function", {}).get("name", "?") for t in chat_req.get("tools", [])])
    return chat_req


# ===================================================================
# Response translation: Chat Completions SSE -> Responses API SSE
# ===================================================================

async def translate_stream(
    upstream: httpx.Response,
    resp_id: str,
    model: str,
) -> Any:
    """
    Consume the upstream Chat Completions SSE stream and yield
    Responses API SSE events.
    """

    # -- Phase 0: response.created --
    yield _sse("response.created", {
        "type": "response.created",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": _now(),
            "status": "in_progress",
            "model": model,
            "output": [],
        },
    })

    # Track state
    output_index = 0
    text_started = False
    full_text = ""
    tool_calls_map: dict[int, dict[str, Any]] = {}
    usage_info: dict[str, Any] = {}
    last_content = ""
    repeat_count = 0
    chunk_count = 0
    loop_broken = False

    async for line in upstream.aiter_lines():
        if not line:
            continue

        if not line.startswith("data: "):
            continue

        payload = line[6:]
        if payload.strip() == "[DONE]":
            break

        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("Failed to parse upstream chunk: %s", payload[:200])
            continue

        # Extract usage if present
        if "usage" in chunk and chunk["usage"]:
            usage_info = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            finish_reason = choice.get("finish_reason")

            # -- Handle text content --
            content = delta.get("content")
            if content is not None and content != "":
                chunk_count += 1
                if content == last_content:
                    repeat_count += 1
                    if repeat_count >= LOOP_REPEAT_LIMIT:
                        log.warning("Loop detected (%d repeats), breaking: %s",
                                    repeat_count, content[:100])
                        # Emit corrective text and break
                        if text_started:
                            yield _sse("response.output_text.delta", {
                                "type": "response.output_text.delta",
                                "output_index": output_index,
                                "content_index": 0,
                                "delta": "\n\n[Loop detected - stopping repetitive output.]",
                            })
                        loop_broken = True
                        break
                else:
                    repeat_count = 0
                last_content = content

                if not text_started:
                    yield _sse("response.output_item.added", {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": {
                            "type": "message",
                            "id": _msg_id(),
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    })
                    text_started = True

                full_text += content
                yield _sse("response.output_text.delta", {
                    "type": "response.output_text.delta",
                    "output_index": output_index,
                    "content_index": 0,
                    "delta": content,
                })

            # -- Handle tool calls --
            tool_calls = delta.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    tc_index = tc.get("index", 0)

                    if tc_index not in tool_calls_map:
                        if text_started:
                            yield _sse("response.output_item.done", {
                                "type": "response.output_item.done",
                                "output_index": output_index,
                                "item": {
                                    "type": "message",
                                    "id": _msg_id(),
                                    "status": "completed",
                                    "role": "assistant",
                                    "content": [
                                        {"type": "output_text", "text": full_text, "annotations": []}
                                    ],
                                },
                            })
                            output_index += 1
                            text_started = False
                            full_text = ""

                        tc_id = tc.get("id", _call_id())
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        arguments = fn.get("arguments", "")

                        tool_calls_map[tc_index] = {
                            "call_id": tc_id,
                            "name": name,
                            "arguments": arguments,
                            "output_index": output_index,
                        }

                        yield _sse("response.output_item.added", {
                            "type": "response.output_item.added",
                            "output_index": output_index,
                            "item": {
                                "type": "function_call",
                                "id": tc_id,
                                "call_id": tc_id,
                                "name": name,
                                "arguments": "",
                                "status": "in_progress",
                            },
                        })
                        output_index += 1
                    else:
                        fn = tc.get("function", {})
                        if fn and fn.get("arguments"):
                            tool_calls_map[tc_index]["arguments"] += fn["arguments"]

            # -- Handle finish --
            if finish_reason:
                if text_started:
                    yield _sse("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": {
                            "type": "message",
                            "id": _msg_id(),
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": full_text, "annotations": []}
                            ],
                        },
                    })
                    output_index += 1
                    text_started = False
                    full_text = ""

                # Close all tool call items with validated arguments
                for idx in sorted(tool_calls_map.keys()):
                    tc = tool_calls_map[idx]
                    validated_args = _validate_and_fix_json(tc["arguments"], tc["name"])
                    yield _sse("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": tc["output_index"],
                        "item": {
                            "type": "function_call",
                            "id": tc["call_id"],
                            "call_id": tc["call_id"],
                            "name": tc["name"],
                            "arguments": validated_args,
                            "status": "completed",
                        },
                    })

        if loop_broken:
            break

    # -- Final: response.completed --
    resp_usage = {
        "input_tokens": usage_info.get("prompt_tokens", 0),
        "output_tokens": usage_info.get("completion_tokens", 0),
        "total_tokens": usage_info.get("total_tokens", 0),
    }

    log.info("Response complete: %d chunks, %d chars text, %d tool calls, loop_broken=%s",
             chunk_count, len(full_text), len(tool_calls_map), loop_broken)

    yield _sse("response.completed", {
        "type": "response.completed",
        "response": {
            "id": resp_id,
            "object": "response",
            "created_at": _now(),
            "status": "completed",
            "model": model,
            "output": [],
            "usage": resp_usage,
        },
    })


# ===================================================================
# Endpoints
# ===================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "upstream": ZAI_BASE_URL}


@app.post("/v1/responses")
async def create_response(request: Request):
    """
    Main endpoint: accept Responses API requests from Codex,
    translate to Chat Completions, forward to Z.ai, translate back.
    """
    if not ZAI_API_KEY:
        return Response(
            content=json.dumps({"error": "ZAI_API_KEY not configured"}),
            status_code=500,
            media_type="application/json",
        )

    try:
        body = await request.json()
    except Exception as exc:
        return Response(
            content=json.dumps({"error": f"Invalid JSON: {exc}"}),
            status_code=400,
            media_type="application/json",
        )

    model = body.get("model", "glm-5.1")
    resp_id = _resp_id()

    # Translate the request
    chat_req = translate_request(body)

    log.info(
        "Forwarding to Z.ai: model=%s, messages=%d, stream=%s",
        chat_req.get("model"),
        len(chat_req.get("messages", [])),
        chat_req.get("stream", True),
    )

    # Forward to Z.ai with streaming
    headers = {
        "Authorization": f"Bearer {ZAI_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    url = f"{ZAI_BASE_URL}/chat/completions"

    # Acquire rate limiter token before hitting upstream
    await _rate_limiter.acquire()

    # Forward to Z.ai with retry on 429
    upstream = None
    last_status = 0
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 2):  # 1 initial + up to 3 retries
        try:
            upstream = await _http_client.send(
                _http_client.build_request("POST", url, json=chat_req, headers=headers),
                stream=True,
            )
        except httpx.ConnectError as exc:
            log.error("Cannot connect to Z.ai: %s", exc)
            return Response(content=json.dumps({"error": "Cannot reach upstream API"}), status_code=502, media_type="application/json")
        except httpx.TimeoutException:
            log.error("Upstream request timed out")
            return Response(content=json.dumps({"error": "Upstream request timed out"}), status_code=504, media_type="application/json")

        last_status = upstream.status_code

        if last_status == 429 and attempt <= RETRY_MAX_ATTEMPTS:
            retry_after = float(upstream.headers.get("Retry-After", "5"))
            # Exponential backoff: 5s, 10s, 20s
            wait = max(retry_after, 2 ** attempt)
            log.warning("429 rate limited (attempt %d/%d), waiting %.1fs", attempt, RETRY_MAX_ATTEMPTS + 1, wait)
            await upstream.aclose()
            await asyncio.sleep(wait)
            continue

        break  # Non-429 or final attempt

    if last_status == 429:
        error_body = await upstream.aread() if upstream else b""
        retry_after = upstream.headers.get("Retry-After", "60") if upstream else "60"
        log.error("429 exhausted retries, Retry-After: %s", retry_after)
        return Response(
            content=json.dumps({"error": {"message": "Rate limited by upstream API", "type": "rate_limit_error", "code": "429"}}),
            status_code=429,
            headers={"Retry-After": str(retry_after)},
            media_type="application/json",
        )

    if last_status != 200:
        error_body = await upstream.aread()
        log.error("Upstream error %d: %s", last_status, error_body[:500])
        return Response(
            content=json.dumps({"error": {"message": f"Upstream returned {last_status}", "type": "upstream_error", "code": str(last_status)}}),
            status_code=last_status,
            media_type="application/json",
        )

    # Stream the translated response back
    return StreamingResponse(
        translate_stream(upstream, resp_id, model),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/v1/models")
async def list_models():
    """Models endpoint for Codex compatibility. Returns known Z.ai models."""
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": _now(),
                "owned_by": "z.ai",
            }
            for m in ZAI_MODELS
        ],
    }


# ===================================================================
# Entry point (for direct execution)
# ===================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "proxy.main:app",
        host="0.0.0.0",
        port=PROXY_PORT,
        log_level=LOG_LEVEL.lower(),
    )
