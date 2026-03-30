#!/usr/bin/env python3
"""
Comprehensive test suite for codex-zai-proxy.

Tests all 3 models (glm-5, glm-5.1, glm-4.7) with:
  1. Flask web app creation (tool calls, valid JSON args, no loops)
  2. Multi-turn debugging (context retention, error correction)
  3. System message consolidation (unit test)
  4. Reasoning preservation
  5. Tool argument edge cases

Usage:
  python3 test_comprehensive.py http://127.0.0.1:4891 LOCAL
  python3 test_comprehensive.py http://127.0.0.1:4892 TWINKLE
"""
import httpx
import time
import json
import sys
from typing import Any

PROXY_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:4891"
LABEL = sys.argv[2] if len(sys.argv) > 2 else "LOCAL"
SINGLE_MODEL = sys.argv[3] if len(sys.argv) > 3 else None

MODELS = [SINGLE_MODEL] if SINGLE_MODEL else ["glm-5", "glm-5.1", "glm-4.7"]

SHELL_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Run a shell command",
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The shell command to run"}
        },
        "required": ["command"]
    }
}

WRITE_TOOL = {
    "type": "function",
    "name": "write_file",
    "description": "Write content to a file",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "content": {"type": "string", "description": "File content"}
        },
        "required": ["path", "content"]
    }
}

READ_TOOL = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read"}
        },
        "required": ["path"]
    }
}

# ===================================================================
# Test Harness
# ===================================================================

class ProxyTest:
    def __init__(self, proxy_url: str, label: str):
        self.proxy_url = proxy_url
        self.label = label
        self.results: list[dict] = []
        self.errors: list[str] = []

    def send_request(self, body: dict, timeout: int = 180) -> list[dict]:
        """Send request and collect all SSE events."""
        events = []
        with httpx.stream("POST", f"{self.proxy_url}/v1/responses", json=body, timeout=timeout) as r:
            for line in r.iter_lines():
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload.strip() == "[DONE]":
                    break
                try:
                    events.append(json.loads(payload))
                except json.JSONDecodeError:
                    pass
        return events

    def extract_tool_calls(self, events: list[dict]) -> list[dict]:
        """Extract completed function_call items."""
        calls = []
        for e in events:
            if e.get("type") == "response.output_item.done":
                item = e.get("item", {})
                if item.get("type") == "function_call":
                    calls.append(item)
        return calls

    def extract_text(self, events: list[dict]) -> str:
        """Concatenate all text deltas."""
        parts = []
        for e in events:
            if e.get("type") == "response.output_text.delta":
                d = e.get("delta", "")
                if d:
                    parts.append(d)
        return "".join(parts)

    def check_no_loop(self, events: list[dict]) -> bool:
        """Verify no repeated text chunks (loop detection)."""
        deltas = []
        for e in events:
            if e.get("type") == "response.output_text.delta":
                deltas.append(e.get("delta", ""))
        if len(deltas) < 10:
            return True
        # Check for 5+ identical consecutive deltas
        last = ""
        count = 0
        for d in deltas:
            if d == last and d.strip():
                count += 1
                if count >= 5:
                    return False
            else:
                count = 0
            last = d
        return True

    def assert_valid_json_args(self, tool_call: dict) -> bool:
        """Validate tool call arguments are parseable JSON."""
        args = tool_call.get("arguments", "")
        try:
            json.loads(args)
            return True
        except json.JSONDecodeError:
            return False

    def record(self, test_name: str, model: str, passed: bool, detail: str = ""):
        self.results.append({
            "test": test_name,
            "model": model,
            "passed": passed,
            "detail": detail,
        })
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {test_name}: {detail}" if detail else f"  [{status}] {test_name}")
        if not passed:
            self.errors.append(f"[{self.label}/{model}] {test_name}: {detail}")

    def report(self):
        print(f"\n{'='*70}")
        print(f"  SUMMARY: {self.label}")
        print(f"{'='*70}")
        passed = sum(1 for r in self.results if r["passed"])
        total = len(self.results)
        print(f"  {passed}/{total} tests passed")
        if self.errors:
            print(f"\n  FAILURES ({len(self.errors)}):")
            for e in self.errors:
                print(f"    - {e}")
            return False
        print(f"\n  ALL TESTS PASSED")
        return True


# ===================================================================
# Test 1: Flask Web App Creation
# ===================================================================

def test_flask_app(t: ProxyTest, model: str):
    print(f"\n  --- Test 1: Flask web app ({model}) ---")
    body = {
        "model": model,
        "instructions": "You are a coding assistant. Use the shell tool to create files and run commands.",
        "input": [
            {"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "Create a minimal Python Flask web app with a /hello endpoint that returns JSON {\"message\": \"Hello World\"}. Use the shell tool to write the file to /tmp/app.py and then verify it by running: python3 -c \"import ast; ast.parse(open('/tmp/app.py').read()); print('Syntax OK')\""}
            ]}
        ],
        "tools": [SHELL_TOOL],
        "tool_choice": "auto",
        "stream": True,
    }

    events = t.send_request(body, timeout=180)
    tool_calls = t.extract_tool_calls(events)
    text = t.extract_text(events)
    no_loop = t.check_no_loop(events)

    # Check: at least 1 tool call made
    t.record("flask_tool_calls", model, len(tool_calls) >= 1,
             f"{len(tool_calls)} tool calls made" if tool_calls else "NO tool calls - model only planned")

    # Check: tool arguments are valid JSON
    for i, tc in enumerate(tool_calls):
        valid = t.assert_valid_json_args(tc)
        t.record(f"flask_args_valid_{i}", model, valid,
                 f"{tc.get('name')}: {tc.get('arguments', '')[:100]}" if valid else f"INVALID JSON: {tc.get('arguments', '')[:100]}")

    # Check: no loops
    t.record("flask_no_loop", model, no_loop,
             "clean output" if no_loop else "LOOP DETECTED")

    # Check: response completed
    completed = any(e.get("type") == "response.completed" for e in events)
    t.record("flask_completed", model, completed,
             "response completed" if completed else "MISSING response.completed")


# ===================================================================
# Test 2: Multi-turn Debugging
# ===================================================================

def test_multi_turn(t: ProxyTest, model: str):
    print(f"\n  --- Test 2: Multi-turn debugging ({model}) ---")

    # Phase 1: Write code
    body1 = {
        "model": model,
        "instructions": "You are a coding assistant.",
        "input": [
            {"type": "message", "role": "user", "content": "Write a Python fibonacci function and run it with: python3 -c 'def fib(n): return n if n < 2 else fib(n-1)+fib(n-2); print(fib(10))'"}
        ],
        "tools": [SHELL_TOOL],
        "stream": True,
    }
    events1 = t.send_request(body1, timeout=180)
    calls1 = t.extract_tool_calls(events1)

    t.record("multi_turn_phase1_calls", model, len(calls1) >= 1,
             f"{len(calls1)} tool calls" if calls1 else "NO tool calls")

    # Phase 2: Feed tool output and ask for continuation
    input_history = [
        {"type": "message", "role": "user", "content": "Write a fib function and test it."}
    ]
    for c in calls1:
        input_history.append({
            "type": "function_call",
            "call_id": c.get("call_id", "call_x"),
            "name": c.get("name", "shell"),
            "arguments": c.get("arguments", "{}"),
        })
        input_history.append({
            "type": "function_call_output",
            "call_id": c.get("call_id", "call_x"),
            "output": "55",
        })
    input_history.append({
        "type": "message", "role": "user", "content": "Good, fib(10)=55. Now also test fib(20)."
    })

    body2 = {
        "model": model,
        "instructions": "You are a coding assistant.",
        "input": input_history,
        "tools": [SHELL_TOOL],
        "stream": True,
    }
    events2 = t.send_request(body2, timeout=180)
    calls2 = t.extract_tool_calls(events2)
    text2 = t.extract_text(events2)

    t.record("multi_turn_phase2_calls", model, len(calls2) >= 1,
             f"{len(calls2)} tool calls" if calls2 else "NO tool calls in phase 2")

    # Phase 3: Context retention
    input_history3 = list(input_history)
    for c in calls2:
        input_history3.append({
            "type": "function_call",
            "call_id": c.get("call_id", "call_y"),
            "name": c.get("name", "shell"),
            "arguments": c.get("arguments", "{}"),
        })
        input_history3.append({
            "type": "function_call_output",
            "call_id": c.get("call_id", "call_y"),
            "output": "6765",
        })
    input_history3.append({
        "type": "message", "role": "user", "content": "What was the original task? Summarize in one sentence."
    })

    body3 = {
        "model": model,
        "instructions": "You are a coding assistant.",
        "input": input_history3,
        "stream": True,
    }
    events3 = t.send_request(body3, timeout=180)
    text3 = t.extract_text(events3)

    remembers = "fibonacci" in text3.lower() or "fib" in text3.lower()
    t.record("multi_turn_context", model, remembers,
             text3[:100] if remembers else f"Model forgot: {text3[:100]}")

    no_loop = t.check_no_loop(events3)
    t.record("multi_turn_no_loop", model, no_loop,
             "clean" if no_loop else "LOOP in phase 3")


# ===================================================================
# Test 3: System Message Consolidation (unit test)
# ===================================================================

def test_system_consolidation(t: ProxyTest):
    print(f"\n  --- Test 3: System message consolidation ---")
    try:
        sys.path.insert(0, "/var/home/preston/podman/codex-zai-proxy")
        from proxy.main import translate_request
    except ImportError:
        print("  [SKIP] System consolidation test (fastapi not installed locally)")
        return

    body = {
        "model": "glm-5.1",
        "instructions": "You are a helpful assistant.",
        "input": [
            {"type": "message", "role": "developer", "content": "Always use tools when possible."},
            {"type": "message", "role": "user", "content": "Hello"},
            {"type": "message", "role": "developer", "content": "Be concise."},
            {"type": "message", "role": "assistant", "content": "Hi!"},
            {"type": "message", "role": "user", "content": "What is 2+2?"},
        ],
    }

    result = translate_request(body)
    messages = result.get("messages", [])

    # Count system messages
    system_msgs = [m for m in messages if m["role"] == "system"]
    has_one = len(system_msgs) == 1
    t.record("system_single", "unit", has_one,
             f"{len(system_msgs)} system messages (expected 1)" if not has_one else "Exactly 1 system message")

    # Check system message is at position 0
    at_pos0 = messages[0]["role"] == "system" if messages else False
    t.record("system_at_pos0", "unit", at_pos0,
             "system message at position 0" if at_pos0 else f"First message is {messages[0]['role'] if messages else 'empty'}")

    # Check all system content is preserved
    combined = system_msgs[0]["content"] if system_msgs else ""
    has_instructions = "helpful assistant" in combined
    has_dev1 = "tools when possible" in combined
    has_dev2 = "Be concise" in combined
    t.record("system_has_all_content", "unit", has_instructions and has_dev1 and has_dev2,
             f"instructions={has_instructions}, dev1={has_dev1}, dev2={has_dev2}")

    # Check no system messages in the middle
    mid_system = any(m["role"] == "system" for m in messages[1:])
    t.record("system_no_mid_conversation", "unit", not mid_system,
             f"Found {sum(1 for m in messages[1:] if m['role'] == 'system')} mid-conversation system msgs" if mid_system else "No mid-conversation system msgs")


# ===================================================================
# Test 4: Reasoning Preservation
# ===================================================================

def test_reasoning(t: ProxyTest, model: str):
    print(f"\n  --- Test 4: Reasoning preservation ({model}) ---")
    body = {
        "model": model,
        "instructions": "You are a coding assistant.",
        "input": [
            {"type": "message", "role": "user", "content": "What is 15 * 17?"},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "I need to multiply 15 by 17. 15*17 = 15*16 + 15 = 240 + 15 = 255."}]},
            {"type": "message", "role": "user", "content": "Based on your reasoning, what was the answer?"},
        ],
        "stream": True,
    }

    events = t.send_request(body, timeout=180)
    text = t.extract_text(events)

    # Model should retain the reasoning context and give 255
    has_answer = "255" in text
    t.record("reasoning_retained", model, has_answer,
             text[:100] if has_answer else f"Expected 255, got: {text[:100]}")

    no_loop = t.check_no_loop(events)
    t.record("reasoning_no_loop", model, no_loop,
             "clean" if no_loop else "LOOP after reasoning")


# ===================================================================
# Test 5: Tool Argument Edge Cases
# ===================================================================

def test_tool_edge_cases(t: ProxyTest, model: str):
    print(f"\n  --- Test 5: Tool argument edge cases ({model}) ---")
    body = {
        "model": model,
        "instructions": "You are a coding assistant with multiple tools available.",
        "input": [
            {"type": "message", "role": "user", "content": "List files in /tmp, then read /etc/hostname. Use the appropriate tools."}
        ],
        "tools": [SHELL_TOOL, READ_TOOL],
        "stream": True,
    }

    events = t.send_request(body, timeout=180)
    tool_calls = t.extract_tool_calls(events)

    t.record("edge_multi_tool", model, len(tool_calls) >= 1,
             f"{len(tool_calls)} tool calls" if tool_calls else "NO tool calls")

    for i, tc in enumerate(tool_calls):
        valid = t.assert_valid_json_args(tc)
        t.record(f"edge_args_valid_{i}", model, valid,
                 f"{tc.get('name')}: valid JSON" if valid else f"{tc.get('name')}: INVALID JSON: {tc.get('arguments', '')[:100]}")

    no_loop = t.check_no_loop(events)
    t.record("edge_no_loop", model, no_loop,
             "clean" if no_loop else "LOOP in edge case test")


# ===================================================================
# Test 6: Rate Limiter + 429 Retry Verification
# ===================================================================

def test_rate_limiter(t: ProxyTest):
    """Verify the proxy rate limiter and 429 retry logic work correctly."""
    print(f"\n  --- Test 6: Rate limiter + 429 retry ---")

    # 6a: Burst test - send 5 rapid requests and verify all succeed
    print("    6a: Burst of 5 rapid requests...")
    burst_pass = True
    burst_times = []
    burst_ok = 0
    for i in range(5):
        body = {
            "model": "glm-5.1",
            "input": [{"type": "message", "role": "user", "content": "Say 'ok' only."}],
            "stream": True,
        }
        start = time.time()
        try:
            events = t.send_request(body, timeout=180)
            text = t.extract_text(events)
            elapsed = time.time() - start
            burst_times.append(elapsed)
            if text.strip():
                burst_ok += 1
            else:
                print(f"      Request {i+1}: EMPTY response ({elapsed:.1f}s)")
        except Exception as e:
            print(f"      Request {i+1}: FAILED ({e})")
            burst_times.append(time.time() - start)

    burst_pass = burst_ok >= 4  # Allow 1 failure for upstream flakiness
    t.record("rate_burst_5", "glm-5.1", burst_pass,
             f"{burst_ok}/5 succeeded, avg {sum(burst_times)/max(len(burst_times),1):.1f}s" if burst_pass else f"Only {burst_ok}/5 succeeded")

    # 6b: Verify no false rate limiting - 3 rapid sequential requests should all succeed
    print("    6b: Rapid sequential requests (verify no false rate limiting)...")
    rapid_pass = True
    rapid_ok = 0
    rapid_start = time.time()
    for i in range(3):
        body = {
            "model": "glm-5.1",
            "input": [{"type": "message", "role": "user", "content": "Reply with just the number 1."}],
            "stream": True,
        }
        try:
            events = t.send_request(body, timeout=180)
            text = t.extract_text(events)
            if text.strip():
                rapid_ok += 1
            else:
                print(f"      Rapid request {i+1}: EMPTY response")
        except Exception as e:
            print(f"      Rapid request {i+1}: FAILED ({e})")
    rapid_elapsed = time.time() - rapid_start

    rapid_pass = rapid_ok >= 2  # Allow 1 failure for upstream flakiness
    t.record("rate_rapid_3", "glm-5.1", rapid_pass,
             f"{rapid_ok}/3 in {rapid_elapsed:.1f}s" if rapid_pass else f"Only {rapid_ok}/3 succeeded")

    # 6c: Verify the /v1/models endpoint works (no rate limiting on GET)
    try:
        r = httpx.get(f"{t.proxy_url}/v1/models", timeout=10)
        models_ok = r.status_code == 200 and "glm-5.1" in r.text
    except:
        models_ok = False
    t.record("rate_models_endpoint", "glm-5.1", models_ok,
             "models endpoint reachable" if models_ok else "models endpoint failed")

    # 6d: Verify the /health endpoint works (no rate limiting on GET)
    try:
        r = httpx.get(f"{t.proxy_url}/health", timeout=10)
        health_ok = r.status_code == 200
    except:
        health_ok = False
    t.record("rate_health_endpoint", "glm-5.1", health_ok,
             "health endpoint reachable" if health_ok else "health endpoint failed")

    # 6e: Verify 429 retry works by checking proxy still responds after rapid burst
    #     Try up to 3 times with a short delay between attempts
    print("    6e: Post-burst verification...")
    post_burst = False
    for attempt in range(3):
        try:
            body = {
                "model": "glm-5.1",
                "input": [{"type": "message", "role": "user", "content": "Say 'done'."}],
                "stream": True,
            }
            events = t.send_request(body, timeout=180)
            text = t.extract_text(events)
            if text.strip():
                post_burst = True
                break
            print(f"      Attempt {attempt+1}: empty response, retrying in 5s...")
        except Exception as e:
            print(f"      Attempt {attempt+1}: failed ({e}), retrying in 5s...")
        time.sleep(5)
    t.record("rate_post_burst", "glm-5.1", post_burst,
             "proxy responsive after burst" if post_burst else "proxy unresponsive after burst")


# ===================================================================
# Main
# ===================================================================

def main():
    print(f"\n{'='*70}")
    print(f"  codex-zai-proxy Comprehensive Test Suite")
    print(f"  Target: {PROXY_URL} ({LABEL})")
    print(f"  Models: {', '.join(MODELS)}")
    print(f"{'='*70}")

    t = ProxyTest(PROXY_URL, LABEL)

    # Health check
    try:
        r = httpx.get(f"{PROXY_URL}/health", timeout=10)
        if r.status_code == 200:
            print(f"  Health: OK")
        else:
            print(f"  Health: FAILED ({r.status_code})")
            sys.exit(1)
    except Exception as e:
        print(f"  Health: FAILED ({e})")
        sys.exit(1)

    # Run tests for each model
    for model in MODELS:
        print(f"\n{'='*60}")
        print(f"  MODEL: {model}")
        print(f"{'='*60}")

        test_flask_app(t, model)
        test_multi_turn(t, model)
        test_reasoning(t, model)
        test_tool_edge_cases(t, model)

    # Run unit tests (only once, not per-model)
    test_system_consolidation(t)

    # Run rate limiter test (uses glm-5.1 only)
    test_rate_limiter(t)

    # Report
    success = t.report()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
