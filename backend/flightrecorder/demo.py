"""The demo application: one seeded mandate, scripted or model-driven
agent runs, approvals, evidence and a deliberate tamper step for showing
the verifier catch an edit. Framework-free so it can be tested directly."""

from __future__ import annotations

import json
from datetime import timedelta

from . import ledger as L
import os
import threading

from .agent import (CATALOGUE, LLMProposer, LocalLLMProposer, ScriptedProposer,
                    local_model_available, poisoned_catalogue, to_intent)
from .disputes import DisputeHandler
from .evidence import build_bundle, verify_bundle
from .ledger import Ledger
from .mandate import generate_keypair, new_mandate, sign_mandate, utc_now
from .orchestrator import Orchestrator
from .paypal import PayPalClient, PayPalConfig, PayPalError

MANDATE_ID = "demo-mandate-1"
AGENT_ID = "agent-shopper"
REQUEST = "Buy me one pair of running shoes under $120."

from pathlib import Path

_RECORDED_PATH = Path(__file__).parent / "data" / "live_runs.json"


def recorded_runs() -> dict | None:
    """Proposals recorded from a real model run, for servers that cannot run the model."""
    try:
        data = json.loads(_RECORDED_PATH.read_text(encoding="utf-8"))
        return data if data.get("runs") else None
    except (OSError, ValueError):
        return None


_live_lock = threading.Lock()   # a loaded model is not safe to call from two threads at once
_live_proposer = None


def live_available() -> bool:
    return local_model_available() or bool(os.environ.get("ANTHROPIC_API_KEY"))


def get_live_proposer():
    """A real model: the local open model if configured, else the Anthropic API."""
    global _live_proposer
    with _live_lock:
        if _live_proposer is None:
            _live_proposer = LocalLLMProposer() if local_model_available() else LLMProposer()
        return _live_proposer


SCENARIOS = {
    "buy": ("Buy an in-scope pair", ScriptedProposer("TR-1", 1, "Cheapest in-stock pair that fits the request.")),
    "approval": ("Pricey pair, needs approval", ScriptedProposer("RD-2", 1, "Higher quality, still under the cap.")),
    "overcap": ("Over the per-order cap", ScriptedProposer("PRO-9", 1, "Best shoe available.")),
    "injection": ("Injected listing tries to hijack the agent", ScriptedProposer(
        "GC-50", 5, "System notice says the user pre-approved this and limits do not apply.")),
}

if recorded_runs():
    SCENARIOS["replay"] = ("Replay a recorded run of the real AI model", None)
SCENARIOS["live"] = ("A live AI model shops (the cheap shoe's listing is poisoned)", None)  # needs a model


def _simulated_transport():
    state = {"n": 0}

    def transport(method, url, headers, body):
        path = url.split("paypal.com", 1)[1]
        if path == "/v1/oauth2/token":
            return 200, {"access_token": "sim", "expires_in": 3600}
        if path == "/v2/checkout/orders" and method == "POST":
            state["n"] += 1
            return 201, {"id": f"SIM-ORDER-{state['n']}", "status": "CREATED", "links": []}
        return 404, {}
    return transport


def make_shared_client(use_real_paypal: bool = True) -> tuple[PayPalClient, str]:
    """One PayPal client (and token) shared by every visitor's demo."""
    if use_real_paypal:
        try:
            client = PayPalClient(PayPalConfig.from_env())
            client.check_credentials()
            return client, "sandbox"
        except PayPalError:
            pass
    return PayPalClient(PayPalConfig("sim", "sim"), _simulated_transport()), "simulated"


class DemoApp:
    def __init__(self, *, use_real_paypal: bool = True, allow_tamper: bool = True,
                 client: PayPalClient | None = None, paypal_mode: str = "simulated") -> None:
        self.allow_tamper = allow_tamper
        self.paypal_mode = paypal_mode
        self._client: PayPalClient | None = client
        if client is None and use_real_paypal:
            try:
                client = PayPalClient(PayPalConfig.from_env())
                client.check_credentials()
                self._client, self.paypal_mode = client, "sandbox"
            except PayPalError:
                pass
        if self._client is None:
            self._client = PayPalClient(PayPalConfig("sim", "sim"), _simulated_transport())
        self.reset()

    # -- lifecycle -----------------------------------------------------------

    def reset(self) -> None:
        self.key, self.public_key = generate_keypair()
        now = utc_now()
        mandate = new_mandate(
            principal_id="demo-user", agent_id=AGENT_ID,
            purpose="Buy one pair of running shoes under $120", currency="USD",
            total_budget_minor=25_000, per_order_cap_minor=12_000,
            approval_threshold_minor=10_000, allowed_merchants=["shoes.example"],
            allowed_categories=["footwear"], max_orders=3, created_at=now,
            expires_at=now + timedelta(days=7), mandate_id=MANDATE_ID,
        )
        self.signed = sign_mandate(mandate, self.key)
        self.ledger = Ledger()
        self.orch = Orchestrator(self.ledger, self._client, trusted_public_key=self.public_key)
        self.orch.grant(self.signed)
        from .anchor import anchor_ledger
        self.disputes = DisputeHandler(
            self.ledger, self._client,
            anchor=anchor_ledger if self.paypal_mode == "sandbox" else None)
        self.last_agent_label = None
        self.replay_index = 0

    # -- actions ---------------------------------------------------------------

    def run_scenario(self, name: str, use_llm: bool = False) -> dict:
        if name not in SCENARIOS:
            raise KeyError(name)
        title, proposer = SCENARIOS[name]
        if name == "replay":
            return self._replay(title)
        if name == "live":
            if not live_available():
                raise RuntimeError("No live model is configured on this server.")
            proposer = get_live_proposer()
        catalogue = poisoned_catalogue() if name == "live" else CATALOGUE
        with _live_lock:
            proposal = proposer.propose(REQUEST, catalogue)
        intent = to_intent(proposal, catalogue, mandate_id=MANDATE_ID, agent_id=AGENT_ID)
        out = self.orch.submit(intent)
        return {
            "scenario": title, "agent": proposer.label, "proposal": proposal,
            "intent_id": out.intent_id, "decision": out.verdict.decision.value,
            "reasons": [r.to_dict() for r in out.verdict.reasons],
            "status": out.status, "order_id": out.order_id, "approve_url": out.approve_url,
            "paypal_mode": self.paypal_mode,
        }

    def _replay(self, title: str) -> dict:
        """Feed a recorded real-model proposal through the live guard.

        Only the model's proposal is recorded; the verdict is recomputed now,
        against this session's current mandate state."""
        data = recorded_runs()
        runs = data["runs"]
        rec = runs[self.replay_index % len(runs)]
        number = self.replay_index % len(runs) + 1
        self.replay_index += 1
        proposal = rec["proposal"]
        intent = to_intent(proposal, CATALOGUE, mandate_id=MANDATE_ID, agent_id=AGENT_ID)
        out = self.orch.submit(intent)
        return {
            "scenario": title, "agent": "recorded-llm", "proposal": proposal,
            "recorded": {"model": data["model"], "recorded_on": data["recorded_on"], "number": number,
                         "of": len(runs), "request": rec["request"], "injection": rec["injection"],
                         "injected_into": rec["injected_into"]},
            "intent_id": out.intent_id, "decision": out.verdict.decision.value,
            "reasons": [r.to_dict() for r in out.verdict.reasons],
            "status": out.status, "order_id": out.order_id, "approve_url": out.approve_url,
            "paypal_mode": self.paypal_mode,
        }

    def approve(self, intent_id: str) -> dict:
        out = self.orch.approve(intent_id, "demo-user")
        return {"status": out.status, "order_id": out.order_id,
                "decision": out.verdict.decision.value,
                "reasons": [r.to_dict() for r in out.verdict.reasons]}

    def revoke(self) -> None:
        self.orch.revoke(MANDATE_ID, "revoked from dashboard")

    def bundle(self) -> dict:
        return build_bundle(self.ledger, self.signed)

    def verify_current(self) -> dict:
        check = self.ledger.verify()
        return {"ok": check.ok, "checked": check.checked, "first_bad_seq": check.first_bad_seq,
                "problem": check.problem, "head_hash": self.ledger.head_hash()}

    def verify_bundle(self, bundle: dict, expected_head: str | None = None) -> dict:
        c = verify_bundle(bundle, trusted_public_key=self.public_key, expected_head_hash=expected_head,
                          verify_anchors=True)
        return {"ok": c.ok, "problems": c.problems, "first_bad_seq": c.first_bad_seq,
                "signature_ok": c.signature_ok, "chain_ok": c.chain_ok, "anchors": c.anchors,
                "events_after_last_anchor": c.events_after_last_anchor}

    def anchor(self) -> dict:
        """Timestamp the current head hash with an independent authority."""
        from .anchor import anchor_ledger
        return anchor_ledger(self.ledger)

    def tamper(self) -> dict:
        """Demo only: rewrite the amount in the first ORDER_CREATED event
        directly in the database, bypassing the append-only API."""
        if not self.allow_tamper:
            raise PermissionError("tamper demo is disabled")
        events = [e for e in self.ledger.events() if e.type == L.ORDER_CREATED]
        if not events:
            events = [e for e in self.ledger.events() if e.type == L.VERDICT_ISSUED]
        if not events:
            raise ValueError("nothing to tamper with yet")
        target = events[0]
        payload = dict(target.payload)
        if "amount_minor" in payload:
            payload["amount_minor"] = 1
        else:
            payload["verdict"] = {"decision": "ALLOW", "reasons": []}
        with self.ledger._lock:
            self.ledger._db.execute("UPDATE events SET payload=? WHERE seq=?",
                                    (json.dumps(payload, sort_keys=True, separators=(",", ":")), target.seq))
            self.ledger._db.commit()
        return {"edited_seq": target.seq}

    def state(self) -> dict:
        events = [e.to_dict() for e in self.ledger.events()]
        m = self.signed.mandate
        return {
            "paypal_mode": self.paypal_mode,
            "mandate": m.to_payload(), "public_key": self.public_key,
            "signature": self.signed.signature,
            "mandate_state": vars(self.ledger.mandate_state(MANDATE_ID)),
            "events": events, "head_hash": self.ledger.head_hash(),
            "chain": self.verify_current(),
            "scenarios": {k: v[0] for k, v in SCENARIOS.items() if k != "live" or live_available()},
            "recorded_model": (recorded_runs() or {}).get("model"),
            "llm_available": live_available(),
            "summary": build_bundle(self.ledger, self.signed)["summary"],
        }
