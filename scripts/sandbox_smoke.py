"""One real PayPal sandbox order through the full path. Prints no secrets."""
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from flightrecorder.guard import LineItem, PurchaseIntent
from flightrecorder.ledger import Ledger
from flightrecorder.mandate import generate_keypair, new_mandate, sign_mandate, utc_now
from flightrecorder.orchestrator import Orchestrator
from flightrecorder.paypal import PayPalClient, PayPalConfig, PayPalError

key, pub = generate_keypair()
now = utc_now()
mandate = new_mandate(
    principal_id="demo-user", agent_id="agent-shopper", purpose="Smoke test",
    currency="USD", total_budget_minor=15_000, per_order_cap_minor=12_000,
    allowed_merchants=["shoes.example"], allowed_categories=["footwear"],
    expires_at=now + timedelta(hours=1), max_orders=1,
)
orch = Orchestrator(Ledger(), PayPalClient(PayPalConfig.from_env()), trusted_public_key=pub)
orch.grant(sign_mandate(mandate, key))
item = LineItem("Trail runner", "footwear", 8_999, 1)
try:
    out = orch.submit(PurchaseIntent(
        mandate.mandate_id, "agent-shopper", "shoes.example", "USD", (item,), 8_999, "smoke test"))
except PayPalError as error:
    print("PayPal error:", error, "status", error.status); raise SystemExit(1)
print("decision:", out.verdict.decision.value, "| status:", out.status, "| order id:", out.order_id)
print("chain ok:", orch.ledger.verify().ok)
