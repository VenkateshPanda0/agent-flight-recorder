"""Minimal stdlib HTTP server: JSON API, dashboard and PayPal webhooks."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import secrets
import threading
from collections import OrderedDict

from .demo import SCENARIOS, DemoApp, make_shared_client
from .orchestrator import OrchestratorError
from .paypal import PayPalError
from .webhooks import WebhookProcessor

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
MAX_BODY = 2_000_000


class Sessions:
    """One demo per visitor (cookie), so simultaneous visitors cannot reset
    or tamper with each other's demo. Oldest sessions are dropped at the cap."""

    def __init__(self, factory, cap: int = 200) -> None:
        self._factory, self._cap = factory, cap
        self._items: OrderedDict[str, DemoApp] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, sid: str | None) -> tuple[str, DemoApp]:
        with self._lock:
            if sid in self._items:
                self._items.move_to_end(sid)
                return sid, self._items[sid]
            sid = secrets.token_urlsafe(16)
            self._items[sid] = self._factory()
            while len(self._items) > self._cap:
                self._items.popitem(last=False)
            return sid, self._items[sid]


def make_handler(sessions: Sessions, webhooks: WebhookProcessor | None, demo_actions: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "AFR/0.1"

        def log_message(self, fmt, *args):  # no request logging: keeps headers out of logs
            pass

        def _app(self) -> DemoApp:
            cookie = self.headers.get("Cookie", "")
            sid = next((p.split("=", 1)[1] for p in cookie.split("; ") if p.startswith("afr_sid=")), None)
            self._sid, app = sessions.get(sid)
            return app

        def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if getattr(self, "_sid", None):
                self.send_header("Set-Cookie", f"afr_sid={self._sid}; Path=/; HttpOnly; SameSite=Lax")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj) -> None:
            self._send(status, json.dumps(obj).encode())

        def _read(self) -> dict | None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._json(413, {"error": "body too large"})
                return None
            try:
                return json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "invalid JSON"})
                return None

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/favicon.ico":
                return self._send(204, b"", "image/x-icon")
            if path == "/healthz":
                return self._json(200, {"ok": True})
            app = self._app()
            if path in ("/", "/index.html"):
                self._send(200, (FRONTEND / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._json(200, app.state())
            elif path == "/api/verify":
                self._json(200, app.verify_current())
            elif path == "/api/bundle":
                self._json(200, app.bundle())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            path = self.path.split("?")[0]
            data = self._read()
            if data is None:
                return
            app = self._app()
            try:
                if path == "/webhooks/paypal":
                    if webhooks is None:
                        return self._json(503, {"error": "webhooks not configured"})
                    status, reason = webhooks.handle(dict(self.headers), data)
                    return self._json(status, {"result": reason})
                if path == "/api/verify-bundle":
                    return self._json(200, app.verify_bundle(data.get("bundle", {}),
                                                             data.get("expected_head_hash")))
                if not demo_actions:
                    return self._json(403, {"error": "demo actions are disabled"})
                if path == "/api/demo/run":
                    if data.get("scenario") not in SCENARIOS:
                        return self._json(400, {"error": "unknown scenario"})
                    return self._json(200, app.run_scenario(data["scenario"], bool(data.get("llm"))))
                if path == "/api/demo/approve":
                    return self._json(200, app.approve(str(data.get("intent_id", ""))))
                if path == "/api/demo/revoke":
                    app.revoke()
                    return self._json(200, {"ok": True})
                if path == "/api/demo/tamper":
                    return self._json(200, app.tamper())
                if path == "/api/demo/reset":
                    app.reset()
                    return self._json(200, {"ok": True})
                if path == "/api/demo/dispute-evidence":
                    dispute_id = str(data.get("dispute_id", ""))
                    res = app.disputes.submit_evidence(dispute_id, app.signed)
                    return self._json(200, {"bundle_digest": res["bundle_digest"]})
                return self._json(404, {"error": "not found"})
            except (OrchestratorError, PayPalError, KeyError, ValueError, RuntimeError, PermissionError) as exc:
                return self._json(400, {"error": str(exc)[:300]})
            except Exception:  # never leak internals
                return self._json(500, {"error": "internal error"})

    return Handler


def main(argv: list[str] | None = None) -> None:
    port = int(os.environ.get("PORT", "8000"))
    demo_actions = os.environ.get("DEMO_ACTIONS", "1") == "1"
    client, mode = make_shared_client(os.environ.get("DEMO_PAYPAL", "real") != "simulated")
    sessions = Sessions(lambda: DemoApp(client=client, paypal_mode=mode, allow_tamper=demo_actions))
    # Webhooks write to a dedicated app (the "owner" ledger), not a visitor's demo.
    owner = DemoApp(client=client, paypal_mode=mode, allow_tamper=False)
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    webhooks = WebhookProcessor(owner.ledger, client, webhook_id, owner.disputes) if webhook_id else None
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(sessions, webhooks, demo_actions))
    print(f"Agent Flight Recorder on :{port} (PayPal: {mode})", file=sys.stderr)
    server.serve_forever()


if __name__ == "__main__":
    main()
