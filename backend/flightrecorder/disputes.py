"""Dispute handling: link a PayPal dispute to the order it concerns, then
answer it with the evidence bundle.

The notes field is capped well under PayPal's limit, so the full bundle is
attached as a document. The notes carry the plain-language summary plus the
log head hash and the bundle digest, which let anyone check the attached
file against what was submitted.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Callable

from . import ledger as L
from .canonical import digest
from .evidence import build_bundle
from .ledger import Ledger
from .mandate import SignedMandate, utc_now
from .paypal import PayPalClient

NOTES_LIMIT = 2000


class DisputeError(Exception):
    pass


class DisputeHandler:
    def __init__(self, ledger: Ledger, paypal: PayPalClient,
                 clock: Callable[[], datetime] = utc_now) -> None:
        self.ledger, self.paypal, self._clock = ledger, paypal, clock

    def _order_for_dispute(self, dispute: dict) -> tuple[str, str, str]:
        """Return (order_ref, mandate_id, capture_id) for a dispute."""
        ids = set()
        for t in dispute.get("disputed_transactions", []):
            for key in ("seller_transaction_id", "buyer_transaction_id"):
                if t.get(key):
                    ids.add(t[key])
        for e in self.ledger.events():
            if e.type == L.ORDER_CAPTURED and e.payload.get("capture_id") in ids:
                return e.order_ref, e.mandate_id, e.payload["capture_id"]
        raise DisputeError("Dispute does not match any captured order in the log.")

    def open(self, dispute_id: str) -> dict:
        """Fetch a dispute from PayPal, match it to an order and log it."""
        dispute = self.paypal.get_dispute(dispute_id)
        order_ref, mandate_id, capture_id = self._order_for_dispute(dispute)
        already = any(
            e.payload.get("dispute_id") == dispute_id
            for e in self.ledger.events(order_ref=order_ref) if e.type == L.DISPUTE_OPENED
        )
        if not already:
            self.ledger.append(
                L.DISPUTE_OPENED,
                {"dispute_id": dispute_id, "reason": dispute.get("reason"),
                 "status": dispute.get("status"), "capture_id": capture_id},
                mandate_id=mandate_id, order_ref=order_ref, ts=self._clock(),
            )
        return {"dispute": dispute, "order_ref": order_ref, "mandate_id": mandate_id}

    def submit_evidence(self, dispute_id: str, signed: SignedMandate) -> dict:
        info = self.open(dispute_id)
        if info["mandate_id"] != signed.mandate.mandate_id:
            raise DisputeError("Dispute belongs to a different mandate.")
        bundle = build_bundle(self.ledger, signed)
        content = json.dumps(bundle, sort_keys=True).encode()
        bundle_digest = digest(bundle)
        notes = self._notes(bundle, bundle_digest, info["order_ref"])
        payload = {"evidences": [{"evidence_type": "OTHER", "notes": notes}]}
        response = self.paypal.provide_evidence_with_document(
            dispute_id, payload, f"evidence-{signed.mandate.mandate_id}.json", content
        )
        self.ledger.append(
            L.EVIDENCE_SUBMITTED,
            {"dispute_id": dispute_id, "bundle_digest": bundle_digest,
             "bundle_head_hash": bundle["head_hash"], "bytes": len(content)},
            mandate_id=info["mandate_id"], order_ref=info["order_ref"], ts=self._clock(),
        )
        return {"response": response, "bundle": bundle, "bundle_digest": bundle_digest}

    @staticmethod
    def _notes(bundle: dict, bundle_digest: str, order_ref: str) -> str:
        tail = (f"\nLog head hash: {bundle['head_hash']}\nBundle digest: {bundle_digest}\n"
                f"Order: {order_ref}\nThe attached JSON file can be verified offline.")
        lines = "\n".join(bundle["summary"]["plain_language"])
        room = NOTES_LIMIT - len(tail)
        if len(lines) > room:
            lines = lines[: room - 1] + "…"
        return lines + tail
