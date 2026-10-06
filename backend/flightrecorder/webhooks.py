"""PayPal webhooks: verify, de-duplicate, log, and act on dispute events.

A webhook is only believed after PayPal's verify-webhook-signature API says
it is genuine. Verified events are logged once per PayPal event id, so
retries and replays change nothing.
"""

from __future__ import annotations

from typing import Mapping

from . import ledger as L
from .disputes import DisputeError, DisputeHandler
from .ledger import Ledger
from .paypal import PayPalClient, PayPalError


class WebhookProcessor:
    def __init__(self, ledger: Ledger, paypal: PayPalClient, webhook_id: str,
                 disputes: DisputeHandler | None = None) -> None:
        self.ledger, self.paypal, self.webhook_id = ledger, paypal, webhook_id
        self.disputes = disputes

    def _seen(self, event_id: str) -> bool:
        return any(e.payload.get("event_id") == event_id
                   for e in self.ledger.events() if e.type == L.WEBHOOK_RECEIVED)

    def handle(self, headers: Mapping[str, str], event: dict) -> tuple[int, str]:
        """Return (http_status, short_reason)."""
        event_id = event.get("id") if isinstance(event, dict) else None
        if not isinstance(event_id, str) or not event_id:
            return 400, "missing event id"
        try:
            genuine = self.paypal.verify_webhook_signature(
                webhook_id=self.webhook_id, headers=headers, event=event)
        except PayPalError:
            return 503, "could not verify signature"  # PayPal will retry
        if not genuine:
            return 401, "signature not verified"
        if self._seen(event_id):
            return 200, "duplicate"

        etype = str(event.get("event_type", ""))
        resource = event.get("resource") or {}
        related = ((resource.get("supplementary_data") or {}).get("related_ids") or {})
        order_ref = related.get("order_id") or (
            resource.get("id") if etype.startswith("CHECKOUT.ORDER") else None)
        mandate_id = self._mandate_for(order_ref)
        self.ledger.append(
            L.WEBHOOK_RECEIVED,
            {"event_id": event_id, "event_type": etype, "resource_id": resource.get("id")},
            mandate_id=mandate_id, order_ref=order_ref,
        )
        if etype.startswith("CUSTOMER.DISPUTE.") and self.disputes and resource.get("dispute_id"):
            try:
                self.disputes.open(resource["dispute_id"])
            except (DisputeError, PayPalError):
                pass  # unmatched or unreachable; the webhook itself is already logged
        return 200, "ok"

    def _mandate_for(self, order_ref: str | None) -> str | None:
        if not order_ref:
            return None
        for e in self.ledger.events(order_ref=order_ref):
            if e.type == L.ORDER_CREATED:
                return e.mandate_id
        return None
