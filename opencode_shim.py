#!/usr/bin/env python3
"""opencode_shim - route an OpenCode client through the shared Zen pool.

OpenCode's own Zen models are Responses-native, so a stock OpenCode client
sends Responses bodies. The free tier this pool reaches is served on
chat/completions. This shim accepts what OpenCode sends and answers in kind.

    OpenCode --Responses--> opencode_shim (:18894) --chat--> zen-router (:18892)

Point an OpenCode client at it as a custom OpenAI-compatible provider whose
base URL is http://127.0.0.1:18894/v1.

Note on the direction of travel: this serves an OpenCode client that is NOT
using OpenCode's own Zen provider - the shim supplies the gate shape the
upstream free tier requires, which a plain OpenCode client pointed at a local
gateway would not send.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "shared"))

import responses as R  # noqa: E402
from shim_base import ShimHandler, run  # noqa: E402


class OpenCodeShim(ShimHandler):
    """Accepts Responses or chat; answers in the same vocabulary it was asked."""

    def translate(self, body: dict) -> tuple[int, dict | bytes, str]:
        model_in = body.get("model") or ""
        # An OpenCode client sends Responses (`input`/`instructions`). A
        # chat-shaped client (messages) is passed through as-is so the same
        # port serves both without the caller having to know.
        is_responses = "input" in body or "instructions" in body
        if not is_responses:
            chat = dict(body)
            chat["model"] = model_in
            chat["stream"] = False
            status, reply, err = self.upstream_chat(chat)
            if err is not None:
                return status, err, model_in
            return 200, reply, model_in

        caller_tools = {t.get("name") for t in body.get("tools") or []
                        if isinstance(t, dict) and t.get("name")}
        schemas = {t.get("name"): t for t in body.get("tools") or []
                   if isinstance(t, dict) and t.get("name")}

        chat = R.to_chat_body(body)
        chat["model"] = model_in
        status, reply, err = self.upstream_chat(chat)
        if err is not None:
            return status, err, model_in
        return 200, R.fold_chat_to_responses(reply, model_in, schemas, caller_tools), model_in


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18894)
    ap.add_argument("--router", default="http://127.0.0.1:18892")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--log", default=None)
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()
    run(OpenCodeShim, args.host, args.port, args.router, args.api_key,
        args.log, args.timeout, "opencode-shim")
