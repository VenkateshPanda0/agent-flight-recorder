"""Ties the pieces together: intent -> verdict -> log -> PayPal order.

The order of operations is the safety property. The intent and the verdict
are written to the log first; PayPal is only called for an ALLOW (or an
approved NEEDS_APPROVAL, which re-runs the guard). A denied purchase never
reaches PayPal.
"""

from __future__ import annotations

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
    status: str  # ORDER_CREATED | DENIED | PENDING_APPROVAL | ORDER_FAILED | ORDER_UNCONFIRMED | DUPLICATE
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
        self._lock = ledger.operation_lock

    # -- mandates ----------------------------------------------------------

    def grant(self, signed: SignedMandate) -> None:
        with self._lock:
            self._require_intact_ledger()
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
            self._require_intact_ledger()
            if self._find_mandate(mandate_id) is None:
                raise OrchestratorError("Unknown mandate.")
            self.ledger.append(
                L.MANDATE_REVOKED, {"reason": reason}, mandate_id=mandate_id, ts=self._clock()
            )

    def _find_mandate(self, mandate_id: str) -> SignedMandate | None:
        for event in self.ledger.events(mandate_id=mandate_id, event_types=(L.MANDATE_GRANTED,)):
            return SignedMandate.from_dict(event.payload)
        return None

    # -- purchases ---------------------------------------------------------

    def submit(self, intent: PurchaseIntent) -> Outcome:
        try:
            iid = intent_id_of(intent)
        except (TypeError, ValueError):
            return self._reject_unhashable(intent)
        with self._lock:
            self._require_intact_ledger()
            earlier = self._intent_events(iid)
            if earlier and not self._failed_before_order(earlier):
                # Replayed intent: report the earlier result, create nothing.
                return self._existing_outcome(iid, earlier)

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
            self._require_intact_ledger()
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
            self._require_intact_ledger()
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
            self._require_intact_ledger()
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
            self._require_intact_ledger()
            mandate_id = self._order_mandate(order_id)
            events = self.ledger.events(order_ref=order_id)
            if any(e.type in (L.ORDER_VOIDED, L.ORDER_REFUNDED) for e in events):
                raise OrchestratorError("A voided or refunded order cannot be captured.")
            if any(e.type == L.ORDER_CAPTURED and e.payload.get("status") == "COMPLETED" for e in events):
                raise OrchestratorError("Order has already been captured.")
            if self._capture_unresolved(events):
                raise OrchestratorError("The previous capture must be reconciled before retrying.")
            request_id = f"capture-{order_id}"
            self.ledger.append(L.CAPTURE_REQUESTED, {"request_id": request_id},
                               mandate_id=mandate_id, order_ref=order_id, ts=self._clock())
            try:
                data = self.paypal.capture_order(order_id, request_id=request_id)
            except PayPalError as error:
                if error.status in (400, 401, 403, 404) or error.issues == ("ORDER_NOT_APPROVED",):
                    self.ledger.append(L.CAPTURE_REJECTED, {"status": error.status},
                                       mandate_id=mandate_id, order_ref=order_id, ts=self._clock())
                raise
            created = next(e.payload for e in events if e.type == L.ORDER_CREATED)
            try:
                unit, = data["purchase_units"]
                capture, = unit["payments"]["captures"]
                currency = capture["amount"]["currency_code"]
                amount = parse_amount(capture["amount"]["value"], currency)
                capture_id = capture["id"]
                status = capture["status"]
                if (not isinstance(capture_id, str) or not capture_id.strip()
                        or status not in ("COMPLETED", "PENDING")
                        or currency != created["currency"] or amount != created["amount_minor"]):
                    raise ValueError
            except (KeyError, TypeError, ValueError, PayPalError):
                raise OrchestratorError("Capture response could not be confirmed. Budget remains reserved.") from None
            self.ledger.append(
                L.ORDER_CAPTURED,
                {
                    "amount_minor": amount,
                    "currency": currency,
                    "capture_id": capture_id,
                    "status": status,
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
            self._require_intact_ledger()
            mandate_id = self._order_mandate(order_id)
            events = self.ledger.events(order_ref=order_id)
            if self._capture_unresolved(events):
                raise OrchestratorError("Capture outcome is unresolved. Budget remains reserved.")
            if any(e.type == L.ORDER_CAPTURED for e in events):
                raise OrchestratorError("A captured order must be refunded, not voided.")
            self.ledger.append(
                L.ORDER_VOIDED, {"reason": reason}, mandate_id=mandate_id,
                order_ref=order_id, ts=self._clock(),
            )

    def refund(self, order_id: str) -> dict:
        with self._lock:
            self._require_intact_ledger()
            mandate_id = self._order_mandate(order_id)
            if any(e.type == L.ORDER_REFUNDED for e in self.ledger.events(order_ref=order_id)):
                raise OrchestratorError("Order has already been refunded.")
            captured = [
                e for e in self.ledger.events(order_ref=order_id) if e.type == L.ORDER_CAPTURED
            ]
            if not captured:
                raise OrchestratorError("Order has not been captured.")
            cap = captured[-1].payload
            if cap.get("status") != "COMPLETED":
                raise OrchestratorError("Capture is not completed; budget remains reserved.")
            data = self.paypal.refund_capture(
                cap["capture_id"], cap["amount_minor"], cap["currency"],
                request_id=f"refund-{cap['capture_id']}",
            )
            if not isinstance(data, dict) or data.get("status") != "COMPLETED":
                raise OrchestratorError("Refund is not completed; budget remains reserved.")
            try:
                refund_id = data["id"]
                currency = data["amount"]["currency_code"]
                amount = parse_amount(data["amount"]["value"], currency)
                if (not isinstance(refund_id, str) or not refund_id.strip()
                        or currency != cap["currency"] or amount != cap["amount_minor"]):
                    raise ValueError
            except (KeyError, TypeError, ValueError, PayPalError):
                raise OrchestratorError("Refund confirmation does not match the request. Budget remains reserved.") from None
            self.ledger.append(
                L.ORDER_REFUNDED,
                {"amount_minor": amount, "currency": currency, "refund_id": refund_id},
                mandate_id=mandate_id, order_ref=order_id, ts=self._clock(),
            )
            return data

    # -- internals ---------------------------------------------------------

    def _require_intact_ledger(self) -> None:
        check = self.ledger.verify()
        if not check.ok:
            raise OrchestratorError(f"Ledger integrity check failed at event #{check.first_bad_seq}.")

    @staticmethod
    def _capture_unresolved(events: list[L.Event]) -> bool:
        for event in reversed(events):
            if event.type == L.CAPTURE_REQUESTED:
                return True
            if event.type == L.CAPTURE_REJECTED:
                return False
            if event.type == L.ORDER_CAPTURED:
                return event.payload.get("status") != "COMPLETED"
        return False

    def _request_id(self, iid: str, intent: PurchaseIntent) -> str:
        """The PayPal-Request-Id: stable for a replay of the same intent under the same signed
        mandate (so retries cannot double-order), but different for every mandate. Using the
        intent id alone would make PayPal hand back an old order, with an old log head in its
        description, whenever two mandates (or two demo sessions) buy the same thing."""
        signed = self._find_mandate(intent.mandate_id)
        return digest({"intent": iid, "mandate": digest(signed.to_dict()) if signed else None})

    def _create_order(self, iid: str, intent: PurchaseIntent, verdict: Verdict) -> Outcome:
        request_id = self._request_id(iid, intent)
        amount = intent.computed_total_minor
        # Commit before the network call so a crash cannot erase the reservation.
        request = self.ledger.append(
            L.ORDER_REQUESTED, {"intent_id": iid, "amount_minor": amount, "request_id": request_id},
            mandate_id=intent.mandate_id, ts=self._clock(),
        )
        head = request.hash
        try:
            order = self.paypal.create_order(
                items=[i.to_dict() for i in intent.items],
                currency=intent.currency,
                mandate_id=intent.mandate_id,
                reference_id=iid[:64],
                description=f"AFR log head {head}"[:127],
                request_id=request_id,
            )
            order_id = order.get("id") if isinstance(order, dict) else None
            if not isinstance(order_id, str) or not order_id.strip():
                raise PayPalError("PayPal response did not identify the order.")
        except PayPalError as error:
            rejected = error.status in (400, 401, 403, 404, 422)
            self.ledger.append(
                L.ORDER_FAILED if rejected else L.ORDER_UNCONFIRMED,
                {"intent_id": iid, "error": str(error), "status": error.status},
                mandate_id=intent.mandate_id, ts=self._clock(),
            )
            return Outcome(iid, verdict, "ORDER_FAILED" if rejected else "ORDER_UNCONFIRMED")
        links = order.get("links")
        approve_url = next(
            (link["href"] for link in links if isinstance(link, dict)
             and link.get("rel") in ("approve", "payer-action") and isinstance(link.get("href"), str)),
            None,
        ) if isinstance(links, list) else None
        self.ledger.append(
            L.ORDER_CREATED,
            {
                "intent_id": iid,
                "amount_minor": amount,
                "currency": intent.currency,
                "merchant_id": intent.merchant_id,
                "paypal_status": order.get("status"),
                "log_head_at_creation": head,
            },
            mandate_id=intent.mandate_id, order_ref=order_id, ts=self._clock(),
        )
        return Outcome(iid, verdict, "ORDER_CREATED", order_id, approve_url)

    def _intent_events(self, iid: str) -> list[L.Event]:
        return self.ledger.intent_events(iid)

    @staticmethod
    def _failed_before_order(events: list[L.Event]) -> bool:
        """Retry only the latest failed attempt, never a later rejection.

        Remote timeout outcomes still need reconciliation; PayPal's request-id
        retention is finite. This predicate only describes the local log.
        """
        return bool(events) and events[-1].type == L.ORDER_FAILED and not any(
            e.type == L.ORDER_CREATED for e in events)

    def _existing_outcome(self, iid: str, events: list[L.Event]) -> Outcome:
        verdict_event = next((e for e in reversed(events) if e.type == L.VERDICT_ISSUED), None)
        verdict = _verdict_from_dict(verdict_event.payload["verdict"]) if verdict_event else \
            self._unknown_mandate_verdict()
        created = next((e for e in events if e.type == L.ORDER_CREATED), None)
        if created:
            return Outcome(iid, verdict, "DUPLICATE", created.order_ref)
        if events and events[-1].type in (L.ORDER_REQUESTED, L.ORDER_UNCONFIRMED):
            return Outcome(iid, verdict, "ORDER_UNCONFIRMED")
        attempt = self._latest_attempt(events)
        if any(e.type == L.APPROVAL_DENIED for e in attempt):
            from .guard import Code, Reason
            verdict = Verdict(Decision.DENY, (Reason(Code.APPROVAL_REJECTED, "The person rejected this purchase."),))
            return Outcome(iid, verdict, "DENIED")
        if verdict.decision is Decision.DENY:
            return Outcome(iid, verdict, "DENIED")
        if verdict.decision is Decision.NEEDS_APPROVAL and not any(
            e.type == L.APPROVAL_GRANTED for e in attempt
        ):
            return Outcome(iid, verdict, "PENDING_APPROVAL")
        return Outcome(iid, verdict, "DUPLICATE")

    @staticmethod
    def _latest_attempt(events: list[L.Event]) -> list[L.Event]:
        for index in range(len(events) - 1, -1, -1):
            if events[index].type == L.INTENT_SUBMITTED:
                return events[index:]
        return events

    def _pending_intent(self, iid: str) -> PurchaseIntent:
        events = self._latest_attempt(self._intent_events(iid))
        submitted = next((e for e in events if e.type == L.INTENT_SUBMITTED), None)
        verdict = next((e for e in reversed(events) if e.type == L.VERDICT_ISSUED), None)
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
