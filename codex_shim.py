#!/usr/bin/env python3
"""codex_shim - let Codex CLI use the shared Zen free-model pool.

Codex CLI v0.157 only speaks the Responses API (`wire_api = "responses"` is
the sole accepted value; `chat` is rejected at config load). The pool serves
chat/completions. This shim accepts Responses from Codex and translates.

    Codex --Responses--> codex_shim (:18893) --chat--> zen-router (:18892)

Configure Codex:

    [model_providers.zen]
    name = "Zen"
    base_url = "http://127.0.0.1:18893/v1"
    wire_api = "responses"
    env_key = "ZEN_SHIM_API_KEY"
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "shared"))

import responses as R  # noqa: E402
from shim_base import ShimHandler, run  # noqa: E402


class CodexShim(ShimHandler):
    """Responses in, chat out. One translation, one upstream call."""

    def translate(self, body: dict) -> tuple[int, dict | bytes, str]:
        model_in = body.get("model") or ""
        caller_tools = {t.get("name") for t in body.get("tools") or []
                        if isinstance(t, dict) and t.get("name")}
        schemas = {t.get("name"): t for t in body.get("tools") or []
                   if isinstance(t, dict) and t.get("name")}

        chat = R.to_chat_body(body)
        chat["model"] = model_in

        status, reply, err = self.upstream_chat(chat)
        if err is not None:
            return status, err, model_in

        resp_obj = R.fold_chat_to_responses(reply, model_in, schemas, caller_tools)
        return 200, resp_obj, model_in


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18893)
    ap.add_argument("--router", default="http://127.0.0.1:18892")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    run(CodexShim, args.host, args.port, args.router, args.api_key,
        args.log, args.timeout, "codex-shim")
