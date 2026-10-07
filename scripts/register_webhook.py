"""Register the deployed /webhooks/paypal URL with the PayPal sandbox.

  python scripts/register_webhook.py https://your-app.onrender.com/webhooks/paypal

Prints the webhook id to put in PAYPAL_WEBHOOK_ID. Prints no secrets.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from flightrecorder.paypal import PayPalClient, PayPalConfig, PayPalError

EVENTS = ["CHECKOUT.ORDER.APPROVED", "PAYMENT.CAPTURE.COMPLETED", "PAYMENT.CAPTURE.REFUNDED",
          "CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED"]
if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
    raise SystemExit(__doc__)
try:
    hook = PayPalClient(PayPalConfig.from_env()).create_webhook(sys.argv[1], EVENTS)
except PayPalError as e:
    raise SystemExit(f"failed: HTTP {e.status} {e}")
print("PAYPAL_WEBHOOK_ID =", hook["id"])
