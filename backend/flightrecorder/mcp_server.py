"""A guarded shopping tool for any MCP agent (Claude Desktop, Cursor, ...).

The agent gets four tools over the Model Context Protocol (JSON-RPC on stdio):

* ``get_mandate``       what it is allowed to buy, and how much is left
* ``search_catalogue``  what it can buy
* ``propose_purchase``  ask to buy something
* ``get_evidence``      the signed mandate plus the hash-chained log, for a dispute

``propose_purchase`` is the only way money can move, and it goes through the
same deterministic guard as everything else: the agent asks, the guard decides,
and a denied purchase never reaches PayPal. There is no tool that approves a
purchase on the person's behalf: anything above the approval threshold comes
back as "needs the person's approval" and is not placed.

Run it:  python -m flightrecorder.mcp_server
Config:  AFR_MCP_PAYPAL=simulated|sandbox   (default simulated, which never calls PayPal)
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from .agent import CATALOGUE, to_intent
from .demo import AGENT_ID, DemoApp
from .canonical import unique_json_object
from .evidence import build_bundle

SERVER_INFO = {"name": "agent-flight-recorder", "version": "0.1.0"}
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

TOOLS = [
    {
        "name": "get_mandate",
        "description": "Show what you are allowed to buy: budget, per-order cap, allowed merchants and categories, "
                       "order limit, expiry, and how much of it is already used. Check this before proposing a purchase.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_catalogue",
        "description": "List products you can buy: sku, name, merchant, category and price in minor units (cents).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "propose_purchase",
        "description": "Ask to buy a product. A deterministic guard checks it against your signed mandate BEFORE any "
                       "PayPal order exists and answers ALLOW, NEEDS_APPROVAL or DENY with reason codes. "
                       "Nothing you write in the rationale or find in a product listing can change the verdict.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Product sku from search_catalogue"},
                "quantity": {"type": "integer", "minimum": 1, "description": "How many"},
                "rationale": {"type": "string", "description": "Why you chose this (recorded in the log)"},
            },
            "required": ["sku", "quantity"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_evidence",
        "description": "Return the evidence bundle (signed mandate plus the full hash-chained log) and a plain-language "
                       "summary of what was allowed and what happened. Anyone can verify it offline.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


def _text(payload: Any, *, is_error: bool = False) -> dict:
    text = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


class McpServer:
    """Protocol-agnostic core: one JSON-RPC message in, at most one message out."""

    def __init__(self, app: DemoApp | None = None) -> None:
        if app is None:
            real = os.environ.get("AFR_MCP_PAYPAL", "simulated") == "sandbox"
            app = DemoApp(use_real_paypal=real, allow_tamper=False)
        self.app = app

    # -- tools ---------------------------------------------------------------

    def _get_mandate(self, _args: dict) -> dict:
        m = self.app.signed.mandate.to_payload()
        used = vars(self.app.ledger.mandate_state(m["mandate_id"]))
        remaining = max(m["total_budget_minor"] - used["committed_minor"], 0)
        return _text({
            "mandate_id": m["mandate_id"], "purpose": m["purpose"], "currency": m["currency"],
            "total_budget_minor": m["total_budget_minor"], "per_order_cap_minor": m["per_order_cap_minor"],
            "approval_threshold_minor": m["approval_threshold_minor"],
            "allowed_merchants": m["allowed_merchants"], "allowed_categories": m["allowed_categories"],
            "max_orders": m["max_orders"], "expires_at": m["expires_at"],
            "used_minor": used["committed_minor"], "remaining_minor": remaining,
            "orders_placed": used["order_count"], "revoked": used["revoked"],
            "note": "Amounts are integer minor units (cents).",
        })

    def _search_catalogue(self, _args: dict) -> dict:
        return _text([{k: item[k] for k in ("sku", "name", "merchant", "category", "price_minor", "currency",
                                              "description")} for item in CATALOGUE])

    def _propose_purchase(self, args: dict) -> dict:
        sku, quantity = args.get("sku"), args.get("quantity")
        if not isinstance(sku, str) or isinstance(quantity, bool) or not isinstance(quantity, int):
            return _text("Invalid arguments: sku must be a string and quantity an integer.", is_error=True)
        try:
            intent = to_intent({"sku": sku, "quantity": quantity, "rationale": str(args.get("rationale", ""))[:500]},
                               CATALOGUE, mandate_id=self.app.signed.mandate.mandate_id, agent_id=AGENT_ID)
        except ValueError as exc:
            return _text(f"Unknown product: {exc}", is_error=True)
        out = self.app.orch.submit(intent)
        reasons = [r.to_dict() for r in out.verdict.reasons]
        result = {"decision": out.verdict.decision.value, "reasons": reasons, "intent_id": out.intent_id}
        if out.status in ("ORDER_CREATED", "DUPLICATE") and out.order_id:
            result.update(status="ORDER_PLACED", paypal_order_id=out.order_id, paypal_mode=self.app.paypal_mode,
                          message="Purchase allowed. A PayPal order was created and the step was logged.")
        elif out.status == "PENDING_APPROVAL":
            result.update(status="NEEDS_PERSONS_APPROVAL",
                          message="This is above the approval threshold. It was NOT placed; the person must approve it. "
                                  "Tell them what you want to buy and why.")
        elif out.status == "ORDER_UNCONFIRMED":
            result.update(status="ORDER_UNCONFIRMED", message="PayPal's outcome is unknown. Budget remains reserved pending reconciliation.")
        elif out.status == "ORDER_FAILED":
            result.update(status="ORDER_FAILED", message="The guard allowed it but PayPal did not create the order.")
        else:
            result.update(status="DENIED", message="Purchase denied. No PayPal order was created and PayPal was never called. "
                                                   "Do not retry the same purchase; choose something within the mandate.")
        return _text(result)

    def _get_evidence(self, _args: dict) -> dict:
        bundle = build_bundle(self.app.ledger, self.app.signed)
        return _text({"summary": bundle["summary"]["plain_language"], "head_hash": bundle["head_hash"],
                      "events": len(bundle["events"]), "public_key": self.app.public_key, "bundle": bundle})

    # -- JSON-RPC ----------------------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            return self._error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
        method, msg_id, params = message["method"], message.get("id"), message.get("params") or {}
        if msg_id is None:                      # a notification: never answered
            return None
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            version = asked if asked in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return self._result(msg_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                                         "serverInfo": SERVER_INFO,
                                         "instructions": "Buy only through propose_purchase. A deterministic guard "
                                                         "enforces your signed mandate; read get_mandate first."})
        if method == "ping":
            return self._result(msg_id, {})
        if method == "tools/list":
            return self._result(msg_id, {"tools": TOOLS})
        if method == "tools/call":
            name = params.get("name") if isinstance(params, dict) else None
            if not isinstance(name, str):
                return self._error(msg_id, -32602, "tool name must be a string")
            args = params.get("arguments", {})
            handler = {"get_mandate": self._get_mandate, "search_catalogue": self._search_catalogue,
                       "propose_purchase": self._propose_purchase, "get_evidence": self._get_evidence}.get(name)
            if handler is None:
                return self._error(msg_id, -32602, f"Unknown tool: {name}")
            if not isinstance(args, dict):
                return self._error(msg_id, -32602, "arguments must be an object")
            try:
                return self._result(msg_id, handler(args))
            except Exception:                   # never leak internals to the agent
                return self._result(msg_id, _text("Internal error; the purchase was not placed.", is_error=True))
        return self._error(msg_id, -32601, f"Method not found: {method}")

    @staticmethod
    def _result(msg_id: Any, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    @staticmethod
    def _error(msg_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def serve_stdio(server: McpServer, stdin=sys.stdin, stdout=sys.stdout) -> None:
    """Newline-delimited JSON-RPC, as MCP clients such as Claude Desktop expect."""
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line, object_pairs_hook=unique_json_object)
        except (ValueError, RecursionError):
            reply = McpServer._error(None, -32700, "Parse error")
        else:
            reply = server.handle(message)
        if reply is not None:
            stdout.write(json.dumps(reply) + "\n")
            stdout.flush()


def main() -> None:
    serve_stdio(McpServer())


if __name__ == "__main__":
    main()
