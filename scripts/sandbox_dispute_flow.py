"""Real PayPal sandbox dispute flow, split in two because a sandbox buyer
must approve the order in a browser.

  python scripts/sandbox_dispute_flow.py start    # creates a guarded order, prints approve link
  (open the link, log in as a SANDBOX buyer, approve, come back)
  python scripts/sandbox_dispute_flow.py finish   # capture, dispute, evidence

State is kept in .afr_flow.json (git-ignored). Secrets are never printed.
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from flightrecorder.disputes import DisputeHandler
from flightrecorder.guard import LineItem, PurchaseIntent
from flightrecorder.ledger import Ledger
from flightrecorder.mandate import SignedMandate, generate_keypair, new_mandate, sign_mandate, utc_now
from flightrecorder.orchestrator import Orchestrator
from flightrecorder.paypal import PayPalClient, PayPalConfig, PayPalError

STATE = ROOT / ".afr_flow.json"
DB = ROOT / ".afr_flow.sqlite"


def build():
    client = PayPalClient(PayPalConfig.from_env())
    return client, Ledger(str(DB))


def start():
    key, pub = generate_keypair()
    now = utc_now()
    mandate = new_mandate(
        principal_id="demo-user", agent_id="agent-shopper", purpose="Buy one pair of running shoes under $120",
        currency="USD", total_budget_minor=25_000, per_order_cap_minor=12_000,
        allowed_merchants=["shoes.example"], allowed_categories=["footwear"],
        expires_at=now + timedelta(days=7), max_orders=3,
    )
    signed = sign_mandate(mandate, key)
    client, ledger = build()
    orch = Orchestrator(ledger, client, trusted_public_key=pub)
    orch.grant(signed)
    item = LineItem("Trail runner", "footwear", 8_999, 1)
    out = orch.submit(PurchaseIntent(mandate.mandate_id, "agent-shopper", "shoes.example", "USD",
                                     (item,), 8_999, "Cheapest in-stock pair."))
    if not out.order_id:
        raise SystemExit(f"order not created: {out.status}")
    STATE.write_text(json.dumps({"signed": signed.to_dict(), "order_id": out.order_id}))
    print("Order created:", out.order_id)
    print("1) Open this link, log in as a SANDBOX BUYER account, and approve the payment:")
    print("  ", out.approve_url)
    print("2) Then run:  python scripts/sandbox_dispute_flow.py finish")


def finish():
    st = json.loads(STATE.read_text())
    client, ledger = build()
    signed = SignedMandate.from_dict(st["signed"])
    orch = Orchestrator(ledger, client)
    order = client.get_order(st["order_id"])
    print("Order status:", order.get("status"))
    if order.get("status") != "APPROVED":
        raise SystemExit("Order is not approved yet; approve it in the browser first.")
    payer_id = order["payer"]["payer_id"]
    cap = orch.capture(st["order_id"])
    capture_id = cap["purchase_units"][0]["payments"]["captures"][0]["id"]
    print("Captured:", capture_id)
    try:
        d = client.create_dispute(buyer_transaction_id=capture_id, reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
                                  amount_minor=8_999, currency="USD", buyer_payer_id=payer_id,
                                  note="Demo dispute for Agent Flight Recorder.")
    except PayPalError as e:
        raise SystemExit(f"dispute creation failed: HTTP {e.status} {e} debug_id={e.debug_id}")
    dispute_id = next((l["href"].rsplit("/", 1)[-1] for l in d.get("links", []) if l.get("rel") == "self"), None)
    print("Dispute created:", dispute_id)
    try:
        client.require_evidence(dispute_id, "SELLER_EVIDENCE")
        print("Dispute moved to the seller-evidence stage.")
    except PayPalError as e:
        print("require-evidence skipped:", e.status, e)
    res = DisputeHandler(ledger, client).submit_evidence(dispute_id, signed)
    print("Evidence submitted. Bundle digest:", res["bundle_digest"])
    print("Dispute status now:", client.get_dispute(dispute_id).get("status"))


if __name__ == "__main__":
    {"start": start, "finish": finish}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
