"""Responses API <-> chat/completions translation.

Two clients in this project speak Responses and need chat upstream:

  * Codex CLI - v0.157 only accepts `wire_api = "responses"`; `chat` is
    rejected at config load.
  * OpenCode - its Zen models are Responses-native, so it sends Responses
    bodies, while the free tier it must reach is served on chat/completions.

Both directions live here so the two shims cannot drift apart.
"""
from __future__ import annotations

import json
import time

from gate import HARNESS_TOOLS, HARNESS_NOTE, add_harness_tools  # noqa: F401


# ---------------------------------------------------------------- request ---

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") in ("input_text", "text", "output_text"):
                    parts.append(block.get("text") or "")
        return "".join(parts)
    return ""


def responses_tools_to_chat(tools: list | None) -> list:
    """Responses tools (flat name/description/parameters) -> chat tools."""
    out = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            out.append(tool)
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name")
        if not name:
            continue
        fn = {"name": name}
        desc = tool.get("description") or (tool.get("function") or {}).get("description")
        if desc:
            fn["description"] = desc
        params = tool.get("parameters") or (tool.get("function") or {}).get("parameters")
        if params:
            fn["parameters"] = params
        out.append({"type": "function", "function": fn})
    return out


def to_chat_body(body: dict, add_harness: bool = True) -> dict:
    """A Responses request body -> a chat/completions body.

    `instructions` becomes a system message. `input` items become messages;
    `function_call` / `function_call_output` items become an assistant
    tool_calls turn and a tool message, which is what a multi-turn agent loop
    looks like on the wire.
    """
    messages = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": instructions})

    for item in body.get("input") or []:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        itype = item.get("type")
        if itype == "function_call":
            messages.append({
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": item.get("call_id") or item.get("id") or "call_0",
                    "type": "function",
                    "function": {"name": item.get("name") or "",
                                 "arguments": item.get("arguments") or "{}"}}]})
            continue
        if itype == "function_call_output":
            out = item.get("output")
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id") or item.get("id") or "call_0",
                "content": out if isinstance(out, str) else json.dumps(out)})
            continue
        text = _text_of(item.get("content"))
        if text:
            messages.append({"role": item.get("role") or "user", "content": text})

    chat: dict = {"messages": messages}
    tools = responses_tools_to_chat(body.get("tools"))
    chat["tools"] = add_harness_tools(tools) if add_harness else tools

    for field in ("tool_choice", "parallel_tool_calls"):
        if body.get(field) is not None:
            chat[field] = body[field]
    if body.get("max_output_tokens"):
        chat["max_tokens"] = body["max_output_tokens"]
    # The router forces streaming upstream and folds the reply back for a
    # non-streaming caller. Asking it for a stream would hand this module SSE.
    chat["stream"] = False
    return chat


# ------------------------------------------------------------------- reply ---

def sanitize_args(name: str, args_json: str, schemas: dict | None = None) -> str:
    """Repair a tool call's arguments so the client will accept the call.

    Measured against Codex v0.157.1, whose `exec_command` declares
    `justification` but rejects it unless `sandbox_permissions` is present.
    A model that emits `justification` alone loses the whole call. The schema
    comes from the client's own request, so nothing here is specific to one
    tool beyond that one pairing rule.
    """
    try:
        parsed = json.loads(args_json or "{}")
    except ValueError:
        return args_json or "{}"
    if not isinstance(parsed, dict):
        return args_json or "{}"
    props = ((schemas or {}).get(name) or {}).get("properties") or {}
    if props:
        parsed = {k: v for k, v in parsed.items() if k in props}
    if "justification" in parsed and "sandbox_permissions" not in parsed:
        parsed.pop("justification", None)
    return json.dumps(parsed)


def fold_chat_to_responses(obj: dict, model_in: str,
                           schemas: dict | None = None,
                           caller_tools: set | None = None) -> dict:
    """A chat.completion object -> a Responses completion object."""
    choices = obj.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    output = []

    text = message.get("content")
    if isinstance(text, str) and text.strip():
        output.append({
            "type": "message", "id": "msg_0", "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text, "annotations": []}]})

    for i, call in enumerate(message.get("tool_calls") or []):
        fn = call.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        # A harness call that leaked through is not a real tool for this
        # caller: drop it rather than hand the client a call it cannot route.
        if name in HARNESS_TOOLS and name not in (caller_tools or set()):
            continue
        output.append({
            "type": "function_call", "id": f"fc_{i}",
            "call_id": call.get("id") or f"call_{i}", "name": name,
            "arguments": sanitize_args(name, fn.get("arguments") or "{}", schemas),
            "status": "completed"})

    usage = obj.get("usage") or {}
    return {
        "id": obj.get("id") or "resp_0",
        "object": "response",
        "created_at": obj.get("created") or int(time.time()),
        "status": "completed",
        "model": obj.get("model") or model_in,
        "output": output,
        "output_text": "".join(
            b.get("text", "") for item in output if item.get("type") == "message"
            for b in item.get("content") or []),
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0)},
    }


# --------------------------------------------------------------------- sse ---

def sse(obj: dict) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def responses_stream(resp_obj: dict) -> bytes:
    """A Responses SSE stream carrying one finished response.

    The `response.created` envelope is emitted with an EMPTY output array.
    Codex errors with "OutputTextDelta without active item" if a delta arrives
    before an item exists, and it reads the terminal `response.completed` for
    the actual content.
    """
    env = dict(resp_obj)
    env["output"] = []
    out = bytearray()
    out += sse({"type": "response.created", "response": env})
    for i, item in enumerate(resp_obj.get("output") or []):
        out += sse({"type": "response.output_item.added",
                    "output_index": i, "item": item})
        if item.get("type") == "message":
            for block in item.get("content") or []:
                out += sse({"type": "response.output_text.delta",
                            "item_id": item.get("id"), "output_index": i,
                            "content_index": 0, "delta": block.get("text", "")})
        out += sse({"type": "response.output_item.done",
                    "output_index": i, "item": item})
    out += sse({"type": "response.completed", "response": dict(resp_obj)})
    out += b"data: [DONE]\n\n"
    return bytes(out)


# --------------------------------------------------- chat -> responses body --

_CHAT_ONLY = ("messages", "max_tokens", "stream", "stream_options", "n", "stop",
              "response_format", "frequency_penalty", "presence_penalty",
              "logit_bias", "user")

_ROLE_MAP = {"system": "developer", "user": "user", "assistant": "assistant",
             "tool": "tool", "function": "function"}

RESPONSES_MIN_OUTPUT_TOKENS = 16


def to_responses_body(chat: dict) -> dict:
    """A chat/completions body -> a Responses body.

    For an OpenCode client whose Zen models are Responses-native but whose
    free tier must be reached over chat upstream. `system` becomes
    `developer` (the Responses instruction role), and tools are flattened.
    """
    out = {k: v for k, v in chat.items() if k not in _CHAT_ONLY}
    instructions, items = [], []
    for msg in chat.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            text = _text_of(msg.get("content"))
            if text:
                instructions.append(text)
            continue
        items.append({"role": _ROLE_MAP.get(role, "user"),
                      "content": [{"type": "input_text",
                                   "text": _text_of(msg.get("content"))}]})
    out["input"] = items
    if instructions:
        out["instructions"] = "\n\n".join(instructions)

    tools = []
    for tool in chat.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") != "function":
            tools.append(tool)
            continue
        fn = tool.get("function") or {}
        if not fn.get("name"):
            continue
        flat = {"type": "function", "name": fn["name"]}
        if fn.get("description"):
            flat["description"] = fn["description"]
        if fn.get("parameters"):
            flat["parameters"] = fn["parameters"]
        tools.append(flat)
    if tools:
        out["tools"] = tools

    if chat.get("max_tokens"):
        out["max_output_tokens"] = chat["max_tokens"]
    # The Responses API enforces a floor; the chat side has none.
    out["max_output_tokens"] = max(RESPONSES_MIN_OUTPUT_TOKENS,
                                   int(out.get("max_output_tokens") or 0))
    out["stream"] = True
    return out
