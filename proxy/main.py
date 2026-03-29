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
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Codex-Z.ai Proxy", version="1.0.0")

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
            # If already has nested "function" key, pass through
            if "function" in tool:
                chat_tools.append(tool)
            else:
                # Wrap name/description/parameters in "function" key
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
        # Skip built-in tool types (web_search, etc.) that Z.ai won't understand
    return chat_tools


def translate_request(body: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a Responses API request body into a Chat Completions API request body.
    """
    messages: list[dict[str, Any]] = []

    # 1. System prompt from "instructions"
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    # 2. Walk input items and build messages
    # We track the last assistant message to accumulate tool_calls into it.
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
            # Map developer -> system for Z.ai compatibility
            if role == "developer":
                role = "system"
            content = _extract_text(item.get("content"))
            messages.append({"role": role, "content": content})
            if role == "assistant":
                last_assistant_idx = len(messages) - 1
            elif role == "system":
                # system messages don't affect the assistant tracking
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
            # These are OpenAI-specific tool call types.
            # Convert to function_call format for Z.ai.
            action = item.get("action", {})
            call_id = item.get("call_id", _call_id())

            # Determine a function name
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
            # Z.ai doesn't have a direct equivalent; skip reasoning items
            pass

        else:
            log.debug("Skipping unknown input item type: %s", itype)

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

    # Translate tools
    tools = body.get("tools")
    if tools:
        chat_tools = _translate_tools(tools)
        if chat_tools:
            chat_req["tools"] = chat_tools
            chat_req["tool_choice"] = body.get("tool_choice", "auto")
            if body.get("parallel_tool_calls") is not None:
                chat_req["parallel_tool_calls"] = body["parallel_tool_calls"]

    log.info("Translated request: %d messages, model=%s", len(messages), chat_req["model"])
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
    tool_calls_map: dict[int, dict[str, Any]] = {}  # index -> {call_id, name, arguments}
    usage_info: dict[str, Any] = {}

    async for line in upstream.aiter_lines():
        if not line:
            continue

        if not line.startswith("data: "):
            # Could be comment lines or event: lines from upstream; skip
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
                if not text_started:
                    # Emit output_item.added for the message
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
                        # New tool call starting
                        # If we were streaming text, close that message first
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
                        # Accumulate arguments
                        fn = tc.get("function", {})
                        if fn and fn.get("arguments"):
                            tool_calls_map[tc_index]["arguments"] += fn["arguments"]

            # -- Handle finish --
            if finish_reason:
                # Close text message if still open
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

                # Close all tool call items
                for idx in sorted(tool_calls_map.keys()):
                    tc = tool_calls_map[idx]
                    yield _sse("response.output_item.done", {
                        "type": "response.output_item.done",
                        "output_index": tc["output_index"],
                        "item": {
                            "type": "function_call",
                            "id": tc["call_id"],
                            "call_id": tc["call_id"],
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                            "status": "completed",
                        },
                    })

    # -- Final: response.completed --
    resp_usage = {
        "input_tokens": usage_info.get("prompt_tokens", 0),
        "output_tokens": usage_info.get("completion_tokens", 0),
        "total_tokens": usage_info.get("total_tokens", 0),
    }

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

    try:
        upstream = await _http_client.send(
            _http_client.build_request(
                "POST",
                url,
                json=chat_req,
                headers=headers,
            ),
            stream=True,
        )
    except httpx.ConnectError as exc:
        log.error("Cannot connect to Z.ai: %s", exc)
        return Response(
            content=json.dumps({"error": "Cannot reach upstream API"}),
            status_code=502,
            media_type="application/json",
        )
    except httpx.TimeoutException:
        log.error("Upstream request timed out")
        return Response(
            content=json.dumps({"error": "Upstream request timed out"}),
            status_code=504,
            media_type="application/json",
        )

    if upstream.status_code != 200:
        error_body = await upstream.aread()
        log.error("Upstream error %d: %s", upstream.status_code, error_body[:500])
        # Return a Responses API error
        return Response(
            content=json.dumps({
                "error": {
                    "message": f"Upstream returned {upstream.status_code}",
                    "type": "upstream_error",
                    "code": str(upstream.status_code),
                }
            }),
            status_code=upstream.status_code,
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
    """Minimal models endpoint for Codex compatibility."""
    return {
        "object": "list",
        "data": [
            {
                "id": "glm-5.1",
                "object": "model",
                "created": _now(),
                "owned_by": "z.ai",
            }
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
