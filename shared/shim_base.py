"""Shared HTTP plumbing for the client shims.

Every shim is the same shape: accept a request from a client, translate it,
ask the router for a chat completion, translate the reply back. Only the
translation differs, so only the translation lives in each shim.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gate import load_identity  # noqa: E402


class ShimHandler(BaseHTTPRequestHandler):
    """Base handler. Subclasses implement `handle(body) -> (status, obj, model)`."""

    protocol_version = "HTTP/1.1"
    server_version = "agent-shims/0.1"

    router = "http://127.0.0.1:18892"
    identity: dict = {}
    api_key: str | None = None
    timeout = 600
    log_path: str | None = None
    name = "shim"
    # Whether to send the API key upstream. zen-shim doesn't need it (the gate
    # is shape-based, not key-based); the router does.
    send_key = True

    # ------------------------------------------------------------ overrides ---

    def translate(self, body: dict) -> tuple[int, dict | bytes, str]:
        """Subclasses implement this. Named `translate` rather than `handle`
        because BaseHTTPRequestHandler already defines `handle()` for the
        request lifecycle - overriding it with a different signature breaks
        every request with a bare TypeError."""
        raise NotImplementedError

    # ------------------------------------------------------------- plumbing ---

    def log_message(self, *a):
        pass

    def _log(self, **fields):
        if not self.log_path:
            return
        line = " ".join(f"{k}={v}" for k, v in fields.items())
        try:
            with open(self.log_path, "a") as fh:
                fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
        except OSError:
            pass

    def _send_json(self, status: int, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def upstream_chat(self, chat_body: dict):
        """POST a chat body to the router. Returns (status, obj, err|None)."""
        payload = json.dumps(chat_body).encode()
        headers = dict(self.identity)
        headers.update({"content-type": "application/json",
                        "accept-encoding": "identity",
                        "content-length": str(len(payload))})
        if self.send_key:
            key = self.api_key or os.environ.get("ZEN_SHIM_API_KEY")
            if key:
                headers["authorization"] = f"Bearer {key}"
        url = self.router.rstrip("/") + "/v1/chat/completions"
        req = urllib.request.Request(url, data=payload, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, json.loads(r.read()), None
        except urllib.error.HTTPError as e:
            detail = e.read()[:300].decode("utf8", "replace")
            self._log(level="upstream-status", status=e.code)
            return e.code, None, {"error": {
                "type": "UpstreamError",
                "message": f"router returned {e.code}: {detail}"}}
        except Exception as exc:  # noqa: BLE001
            self._log(level="upstream-error", err=type(exc).__name__)
            return 502, None, {"error": {
                "type": "UpstreamUnreachable",
                "message": f"router unreachable ({exc})"}}

    # -------------------------------------------------------------- routing ---

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/healthz", "/health"):
            return self._send_json(200, {"ok": True, "shim": self.name,
                                         "router": self.router,
                                         "identity": bool(self.identity)})
        self._send_json(404, {"error": {"message": f"{self.name}: not found"}})

    def do_POST(self):
        started = time.time()
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            return self._send_json(400, {"error": {
                "message": f"{self.name}: body is not JSON"}})
        try:
            status, obj, model = self.translate(body)
        except Exception as exc:  # noqa: BLE001 - a shim must not 500 silently
            self._log(level="shim-error", err=type(exc).__name__)
            return self._send_json(500, {"error": {
                "type": "ShimError", "message": f"{type(exc).__name__}: {exc}"}})

        self._log(level="ok" if status == 200 else "error", status=status,
                  model=model, ms=int((time.time() - started) * 1000))

        if status != 200:
            return self._send_json(status, obj)
        if isinstance(obj, bytes):
            return self._send_bytes(obj)
        # Codex and OpenCode both read the SSE envelope.
        if "text/event-stream" in (self.headers.get("Accept") or "") or \
                body.get("stream"):
            return self._send_bytes(R_stream(obj))
        return self._send_json(200, obj)

    def _send_bytes(self, data: bytes, ctype="text/event-stream"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def R_stream(obj: dict) -> bytes:
    """Responses SSE envelope, imported lazily so a chat-only shim needs none."""
    import responses as R
    return R.responses_stream(obj)


def run(handler_cls, host, port, router, api_key, log_path, timeout, name):
    handler_cls.router = router.rstrip("/")
    handler_cls.identity = load_identity()
    handler_cls.api_key = api_key
    handler_cls.log_path = log_path
    handler_cls.timeout = timeout
    handler_cls.name = name
    srv = ThreadingHTTPServer((host, port), handler_cls)
    srv.daemon_threads = True
    print(f"{name} on http://{host}:{port} -> {handler_cls.router} "
          f"(identity={'yes' if handler_cls.identity else 'NO'})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
