"""Ties the pieces together: intent -> verdict -> log -> PayPal order.

The order of operations is the safety property. The intent and the verdict
are written to the log first; PayPal is only called for an ALLOW (or an
approved NEEDS_APPROVAL, which re-runs the guard). A denied purchase never
reaches PayPal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from . import ledger as L
from .canonical import digest
from .guard import (
    Decision,
    LineItem,
    PurchaseIntent,
    Verdict,
    evaluate,
)
from .ledger import Ledger
from .mandate import SignedMandate, utc_now
from .paypal import PayPalClient, PayPalError, parse_amount


class OrchestratorError(Exception):
    pass


@dataclass(frozen=True)
class Outcome:
    intent_id: str
    verdict: Verdict
    status: str  # ORDER_CREATED | DENIED | PENDING_APPROVAL | ORDER_FAILED | DUPLICATE
    order_id: str | None = None
    approve_url: str | None = None


def intent_id_of(intent: PurchaseIntent) -> str:
    return digest(intent.to_dict())


def intent_from_dict(data: dict) -> PurchaseIntent:
    return PurchaseIntent(
        mandate_id=data["mandate_id"],
        agent_id=data["agent_id"],
        merchant_id=data["merchant_id"],
        currency=data["currency"],
        items=tuple(LineItem(**item) for item in data["items"]),
        claimed_total_minor=data["claimed_total_minor"],
        rationale=data.get("rationale", ""),
        context_digest=data.get("context_digest"),
    )


class Orchestrator:
    def __init__(
        self,
        ledger: Ledger,
        paypal: PayPalClient,
        *,
        trusted_public_key: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.ledger = ledger
        self.paypal = paypal
        self.trusted_public_key = trusted_public_key
        self._clock = clock
        self._lock = threading.RLock()

    # -- mandates ----------------------------------------------------------

    def grant(self, signed: SignedMandate) -> None:
        with self._lock:
            if self._find_mandate(signed.mandate.mandate_id) is not None:
                raise OrchestratorError("Mandate already granted.")
            self.ledger.append(
                L.MANDATE_GRANTED,
                signed.to_dict(),
                mandate_id=signed.mandate.mandate_id,
                ts=self._clock(),
            )

    def revoke(self, mandate_id: str, reason: str = "") -> None:
        with self._lock:
            if self._find_mandate(mandate_id) is None:
                raise OrchestratorError("Unknown mandate.")
            self.ledger.append(
                L.MANDATE_REVOKED, {"reason": reason}, mandate_id=mandate_id, ts=self._clock()
            )

    def _find_mandate(self, mandate_id: str) -> SignedMandate | None:
        for event in self.ledger.events(mandate_id=mandate_id):
            if event.type == L.MANDATE_GRANTED:
                return SignedMandate.from_dict(event.payload)
        return None

    # -- purchases ---------------------------------------------------------

    def submit(self, intent: PurchaseIntent) -> Outcome:
        try:
            iid = intent_id_of(intent)
        except (TypeError, ValueError):
            return self._reject_unhashable(intent)
        with self._lock:
            if self._intent_events(iid):
                # Replayed intent: report the earlier result, create nothing.
                return self._existing_outcome(iid)

            self.ledger.append(
                L.INTENT_SUBMITTED,
                {"intent_id": iid, "intent": intent.to_dict()},
                mandate_id=intent.mandate_id,
                ts=self._clock(),
            )
            signed = self._find_mandate(intent.mandate_id)
            if signed is None:
                verdict = self._unknown_mandate_verdict()
            else:
                verdict = evaluate(
                    signed,
                    intent,
                    self.ledger.mandate_state(intent.mandate_id),
                    self._clock(),
                    self.trusted_public_key,
                )
            self.ledger.append(
                L.VERDICT_ISSUED,
                {"intent_id": iid, "verdict": verdict.to_dict()},
                mandate_id=intent.mandate_id,
                ts=self._clock(),
            )
            if verdict.decision is Decision.DENY:
                return Outcome(iid, verdict, "DENIED")
            if verdict.decision is Decision.NEEDS_APPROVAL:
                return Outcome(iid, verdict, "PENDING_APPROVAL")
            return self._create_order(iid, intent, verdict)

    def _reject_unhashable(self, intent: PurchaseIntent) -> Outcome:
        """Floats or other unsignable content: refuse, and keep a text record."""
        from .guard import Code, Reason
        verdict = Verdict(
            Decision.DENY,
            (Reason(Code.INVALID_LINE_ITEM, "Intent contains data that cannot be hashed, such as a float."),),
        )
        with self._lock:
            self.ledger.append(
                L.INTENT_SUBMITTED,
                {"unhashable_intent": repr(intent)[:500]},
                mandate_id=intent.mandate_id if isinstance(intent.mandate_id, str) else None,
                ts=self._clock(),
            )
            self.ledger.append(
                L.VERDICT_ISSUED, {"verdict": verdict.to_dict()},
                mandate_id=intent.mandate_id if isinstance(intent.mandate_id, str) else None,
                ts=self._clock(),
            )
        return Outcome("unhashable", verdict, "DENIED")

    def approve(self, intent_id: str, approver: str) -> Outcome:
        with self._lock:
            intent = self._pending_intent(intent_id)
            self.ledger.append(
                L.APPROVAL_GRANTED,
                {"intent_id": intent_id, "approver": approver},
                mandate_id=intent.mandate_id,
                ts=self._clock(),
            )
            # Approval never overrides a hard rule: run the guard again.
            signed = self._find_mandate(intent.mandate_id)
            verdict = evaluate(
                signed,
                intent,
                self.ledger.mandate_state(intent.mandate_id),
                self._clock(),
                self.trusted_public_key,
            )
            self.ledger.append(
                L.VERDICT_ISSUED,
                {"intent_id": intent_id, "verdict": verdict.to_dict(), "after_approval": True},
                mandate_id=intent.mandate_id,
                ts=self._clock(),
            )
            if verdict.decision is Decision.DENY:
                return Outcome(intent_id, verdict, "DENIED")
            return self._create_order(intent_id, intent, verdict)

    def reject(self, intent_id: str, approver: str) -> None:
        with self._lock:
            intent = self._pending_intent(intent_id)
            self.ledger.append(
                L.APPROVAL_DENIED,
                {"intent_id": intent_id, "approver": approver},
                mandate_id=intent.mandate_id,
                ts=self._clock(),
            )

    # -- order lifecycle ---------------------------------------------------

    def capture(self, order_id: str) -> dict:
        with self._lock:
            mandate_id = self._order_mandate(order_id)
            data = self.paypal.capture_order(order_id, request_id=f"capture-{order_id}")
            unit = data["purchase_units"][0]
            capture = unit["payments"]["captures"][0]
            currency = capture["amount"]["currency_code"]
            self.ledger.append(
                L.ORDER_CAPTURED,
                {
                    "amount_minor": parse_amount(capture["amount"]["value"], currency),
                    "currency": currency,
                    "capture_id": capture["id"],
                    "status": capture.get("status"),
                },
                mandate_id=mandate_id,
                order_ref=order_id,
                ts=self._clock(),
            )
            return data

    def void(self, order_id: str, reason: str = "") -> None:
        """Release an unpaid order. Unapproved PayPal orders simply lapse,
        so this records the release and gives the budget and slot back."""
        with self._lock:
            mandate_id = self._order_mandate(order_id)
            self.ledger.append(
                L.ORDER_VOIDED, {"reason": reason}, mandate_id=mandate_id,
                order_ref=order_id, ts=self._clock(),
            )

    def refund(self, order_id: str) -> dict:
        with self._lock:
            mandate_id = self._order_mandate(order_id)
            captured = [
                e for e in self.ledger.events(order_ref=order_id) if e.type == L.ORDER_CAPTURED
            ]
            if not captured:
                raise OrchestratorError("Order has not been captured.")
            cap = captured[-1].payload
            data = self.paypal.refund_capture(
                cap["capture_id"], cap["amount_minor"], cap["currency"],
                request_id=f"refund-{cap['capture_id']}",
            )
            self.ledger.append(
                L.ORDER_REFUNDED,
                {"amount_minor": cap["amount_minor"], "currency": cap["currency"],
                 "refund_id": data.get("id")},
                mandate_id=mandate_id, order_ref=order_id, ts=self._clock(),
            )
            return data

    # -- internals ---------------------------------------------------------

    def _create_order(self, iid: str, intent: PurchaseIntent, verdict: Verdict) -> Outcome:
        head = self.ledger.head_hash()
        try:
            order = self.paypal.create_order(
                items=[i.to_dict() for i in intent.items],
                currency=intent.currency,
                mandate_id=intent.mandate_id,
                reference_id=iid[:64],
                description=f"AFR log head {head}"[:127],
                request_id=iid,
            )
        except PayPalError as error:
            self.ledger.append(
                L.ORDER_FAILED,
                {"intent_id": iid, "error": str(error), "status": error.status},
                mandate_id=intent.mandate_id, ts=self._clock(),
            )
            return Outcome(iid, verdict, "ORDER_FAILED")
        approve_url = next(
            (l["href"] for l in order.get("links", []) if l.get("rel") in ("approve", "payer-action")),
            None,
        )
        self.ledger.append(
            L.ORDER_CREATED,
            {
                "intent_id": iid,
                "amount_minor": intent.computed_total_minor,
                "currency": intent.currency,
                "merchant_id": intent.merchant_id,
                "paypal_status": order.get("status"),
                "log_head_at_creation": head,
            },
            mandate_id=intent.mandate_id, order_ref=order["id"], ts=self._clock(),
        )
        return Outcome(iid, verdict, "ORDER_CREATED", order["id"], approve_url)

    def _intent_events(self, iid: str) -> list[L.Event]:
        return [
            e for e in self.ledger.events()
            if e.payload.get("intent_id") == iid
        ]

    def _existing_outcome(self, iid: str) -> Outcome:
        events = self._intent_events(iid)
        verdict_event = next((e for e in reversed(events) if e.type == L.VERDICT_ISSUED), None)
        verdict = _verdict_from_dict(verdict_event.payload["verdict"]) if verdict_event else \
            self._unknown_mandate_verdict()
        created = next((e for e in events if e.type == L.ORDER_CREATED), None)
        if created:
            return Outcome(iid, verdict, "DUPLICATE", created.order_ref)
        return Outcome(iid, verdict, "DUPLICATE")

    def _pending_intent(self, iid: str) -> PurchaseIntent:
        events = self._intent_events(iid)
        submitted = next((e for e in events if e.type == L.INTENT_SUBMITTED), None)
        verdict = next((e for e in events if e.type == L.VERDICT_ISSUED), None)
        if submitted is None or verdict is None:
            raise OrchestratorError("Unknown intent.")
        if verdict.payload["verdict"]["decision"] != Decision.NEEDS_APPROVAL.value:
            raise OrchestratorError("Intent is not waiting for approval.")
        if any(e.type in (L.APPROVAL_GRANTED, L.APPROVAL_DENIED, L.ORDER_CREATED) for e in events):
            raise OrchestratorError("Intent has already been decided.")
        return intent_from_dict(submitted.payload["intent"])

    def _order_mandate(self, order_id: str) -> str:
        for event in self.ledger.events(order_ref=order_id):
            if event.type == L.ORDER_CREATED:
                return event.mandate_id
        raise OrchestratorError("Unknown order.")

    @staticmethod
    def _unknown_mandate_verdict() -> Verdict:
        from .guard import Code, Reason
        return Verdict(
            Decision.DENY, (Reason(Code.MANDATE_MISMATCH, "No such mandate has been granted."),)
        )


def _verdict_from_dict(data: dict) -> Verdict:
    from .guard import Code, Reason
    return Verdict(
        Decision(data["decision"]),
        tuple(Reason(Code(r["code"]), r["message"]) for r in data["reasons"]),
    )
