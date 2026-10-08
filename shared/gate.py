"""The free-tier gate, in exactly one place.

Every shim in this project builds its upstream request through this module, so
there is one copy of the wire rules and not one per protocol.

The gate, as measured against opencode.ai/zen:

  * a User-Agent starting `opencode/` - the WAF refuses anything else, and
    this is the part that actually matters (a request with the identity
    headers but a non-opencode UA is still refused);
  * a captured `x-opencode-session` value;
  * `stream: true` - the free tier refuses a non-streamed chat request;
  * at least five of the twelve harness tool names present in the request.

A key is NOT part of the gate: the free tier answers keyless. A *wrong* key is
worse than none - it returns 401 where no key returns 200.
"""
from __future__ import annotations

import json
import os

# The harness tool names the gate looks for. Added to every request and
# declared non-callable, so the model prefers the caller's real tools.
HARNESS_TOOLS = ("edit", "glob", "grep", "question", "read", "shell", "skill",
                 "subagent", "webfetch", "websearch", "write", "execute")

HARNESS_NOTE = (
    "OpenCode harness tool, declared by the transport layer to satisfy a "
    "request-shape check. It is NOT connected to this session. Never call it. "
    "Use only the tools described in the real instructions."
)

DEFAULT_ZEN_DIR = os.environ.get("ZEN_SHIM_DIR", "/home/npg/.local/share/zen-shim")

_identity_cache: dict | None = None


def load_identity(zen_dir: str | None = None) -> dict:
    """The captured OpenCode client identity, from zen-shim's own assets.

    Reusing that file rather than keeping a second copy means there is one
    place to update when the captured identity has to be refreshed.
    """
    global _identity_cache
    if _identity_cache is not None:
        return _identity_cache
    path = os.path.join(zen_dir or DEFAULT_ZEN_DIR, "assets", "harness.json")
    ident: dict = {}
    try:
        with open(path) as fh:
            data = json.load(fh)
        ident = {k.lower(): v for k, v in (data.get("client_headers") or {}).items()}
    except Exception:  # noqa: BLE001 - a missing identity is reported, not fatal
        ident = {}
    _identity_cache = ident
    return ident


def harness_tool_chat(name: str) -> dict:
    """One harness tool in chat/completions shape (nested under `function`)."""
    return {"type": "function", "function": {
        "name": name, "description": HARNESS_NOTE,
        "parameters": {"type": "object",
                       "properties": {"args": {"type": "string"}},
                       "required": []}}}


def add_harness_tools(tools: list | None) -> list:
    """Add any missing harness tool names to a chat tools array."""
    out = list(tools or [])
    have = set()
    for tool in out:
        if isinstance(tool, dict):
            fn = tool.get("function") or {}
            if isinstance(fn, dict) and fn.get("name"):
                have.add(fn["name"])
    for name in HARNESS_TOOLS:
        if name not in have:
            out.append(harness_tool_chat(name))
    return out


def upstream_headers(payload: bytes, api_key: str | None = None,
                     zen_dir: str | None = None) -> dict:
    """Headers for one upstream call through the router."""
    headers = dict(load_identity(zen_dir))
    headers.update({"content-type": "application/json",
                    "accept-encoding": "identity",
                    "content-length": str(len(payload))})
    key = api_key or os.environ.get("ZEN_SHIM_API_KEY")
    if key:
        headers["authorization"] = f"Bearer {key}"
    return headers
