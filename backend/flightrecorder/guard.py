"""The guard: decides whether one purchase fits inside a mandate.

This is deliberately plain code with no AI model in it. A model proposes a
purchase; these rules dispose of it. The same inputs always give the same
verdict, and every refusal carries a reason code that goes into the log and,
if the purchase is later disputed, into the evidence.

Checks fail closed: anything unverifiable or out of scope is denied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from .mandate import ANY_CATEGORY, SignedMandate, from_iso, verify_mandate


class Decision(str, Enum):
    ALLOW = "ALLOW"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    DENY = "DENY"


class Code(str, Enum):
    SIGNATURE_INVALID = "SIGNATURE_INVALID"
    MANDATE_MISMATCH = "MANDATE_MISMATCH"
    MANDATE_REVOKED = "MANDATE_REVOKED"
    MANDATE_NOT_YET_VALID = "MANDATE_NOT_YET_VALID"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    AGENT_MISMATCH = "AGENT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    MERCHANT_NOT_ALLOWED = "MERCHANT_NOT_ALLOWED"
    CATEGORY_NOT_ALLOWED = "CATEGORY_NOT_ALLOWED"
    EMPTY_ORDER = "EMPTY_ORDER"
    INVALID_LINE_ITEM = "INVALID_LINE_ITEM"
    TOTAL_MISMATCH = "TOTAL_MISMATCH"
    PER_ORDER_CAP_EXCEEDED = "PER_ORDER_CAP_EXCEEDED"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    MAX_ORDERS_EXCEEDED = "MAX_ORDERS_EXCEEDED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"


@dataclass(frozen=True)
class LineItem:
    name: str
    category: str
    unit_price_minor: int
    quantity: int = 1

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "unit_price_minor": self.unit_price_minor,
            "quantity": self.quantity,
        }


@dataclass(frozen=True)
class PurchaseIntent:
    """What the agent says it wants to buy."""

    mandate_id: str
    agent_id: str
    merchant_id: str
    currency: str
    items: tuple[LineItem, ...]
    claimed_total_minor: int
    rationale: str = ""
    # Hash of the page or tool output the agent acted on. Kept so that a
    # later review can tell what the agent was looking at when it decided.
    context_digest: str | None = None

    @property
    def computed_total_minor(self) -> int:
        return sum(item.unit_price_minor * item.quantity for item in self.items)

    def to_dict(self) -> dict:
        return {
            "mandate_id": self.mandate_id,
            "agent_id": self.agent_id,
            "merchant_id": self.merchant_id,
            "currency": self.currency,
            "items": [item.to_dict() for item in self.items],
            "claimed_total_minor": self.claimed_total_minor,
            "rationale": self.rationale,
            "context_digest": self.context_digest,
        }


@dataclass(frozen=True)
class MandateState:
    """What has already happened under a mandate, taken from the log."""

    committed_minor: int = 0  # captured, plus created-but-not-yet-captured
    order_count: int = 0
    revoked: bool = False


@dataclass(frozen=True)
class Reason:
    code: Code
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code.value, "message": self.message}


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reasons: tuple[Reason, ...] = field(default_factory=tuple)

    @property
    def codes(self) -> tuple[Code, ...]:
        return tuple(reason.code for reason in self.reasons)

    def to_dict(self) -> dict:
        return {
            "decision": self.decision.value,
            "reasons": [reason.to_dict() for reason in self.reasons],
        }


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def evaluate(
    signed: SignedMandate,
    intent: PurchaseIntent,
    state: MandateState,
    now: datetime,
    trusted_public_key: str | None = None,
) -> Verdict:
    """Check ``intent`` against ``signed`` and return a verdict.

    All violations are collected, not just the first, so the log and any
    later dispute evidence show the full picture.
    """
    mandate = signed.mandate

    # Nothing else in the mandate can be trusted if the signature is bad.
    if not verify_mandate(signed, trusted_public_key):
        return Verdict(
            Decision.DENY,
            (Reason(Code.SIGNATURE_INVALID, "Mandate signature does not verify."),),
        )

    hard: list[Reason] = []

    if intent.mandate_id != mandate.mandate_id:
        hard.append(Reason(Code.MANDATE_MISMATCH, "Purchase refers to a different mandate."))
    if state.revoked:
        hard.append(Reason(Code.MANDATE_REVOKED, "Mandate has been revoked."))
    if now < from_iso(mandate.created_at):
        hard.append(Reason(Code.MANDATE_NOT_YET_VALID, "Mandate is not valid yet."))
    if now >= from_iso(mandate.expires_at):
        hard.append(Reason(Code.MANDATE_EXPIRED, f"Mandate expired at {mandate.expires_at}."))
    if intent.agent_id != mandate.agent_id:
        hard.append(
            Reason(Code.AGENT_MISMATCH, "Mandate was granted to a different agent.")
        )
    if intent.currency != mandate.currency:
        hard.append(
            Reason(
                Code.CURRENCY_MISMATCH,
                f"Purchase is in {intent.currency}; mandate allows {mandate.currency}.",
            )
        )

    merchant = intent.merchant_id.strip().lower()
    if merchant not in mandate.allowed_merchants:
        hard.append(
            Reason(Code.MERCHANT_NOT_ALLOWED, f"Merchant '{merchant}' is not on the mandate.")
        )

    if not intent.items:
        hard.append(Reason(Code.EMPTY_ORDER, "Purchase has no items."))

    items_valid = True
    for item in intent.items:
        if not _is_positive_int(item.unit_price_minor) or not _is_positive_int(item.quantity):
            items_valid = False
            hard.append(
                Reason(
                    Code.INVALID_LINE_ITEM,
                    f"Item '{item.name}' has a non-positive price or quantity.",
                )
            )
            continue
        category = item.category.strip().lower()
        if ANY_CATEGORY not in mandate.allowed_categories and (
            category not in mandate.allowed_categories
        ):
            hard.append(
                Reason(
                    Code.CATEGORY_NOT_ALLOWED,
                    f"Item '{item.name}' is in category '{category}', which is not on the mandate.",
                )
            )

    # Amount checks use the total computed from the line items, never the
    # figure the agent claims, so an under-reported total cannot slip through.
    if items_valid and intent.items:
        total = intent.computed_total_minor
        if intent.claimed_total_minor != total:
            hard.append(
                Reason(
                    Code.TOTAL_MISMATCH,
                    f"Claimed total {intent.claimed_total_minor} does not match items total {total}.",
                )
            )
        if total > mandate.per_order_cap_minor:
            hard.append(
                Reason(
                    Code.PER_ORDER_CAP_EXCEEDED,
                    f"Order total {total} exceeds per-order cap {mandate.per_order_cap_minor}.",
                )
            )
        remaining = mandate.total_budget_minor - state.committed_minor
        if total > remaining:
            hard.append(
                Reason(
                    Code.BUDGET_EXCEEDED,
                    f"Order total {total} exceeds remaining budget {max(remaining, 0)}.",
                )
            )

    if state.order_count >= mandate.max_orders:
        hard.append(
            Reason(
                Code.MAX_ORDERS_EXCEEDED,
                f"Mandate allows {mandate.max_orders} order(s); {state.order_count} already placed.",
            )
        )

    if hard:
        return Verdict(Decision.DENY, tuple(hard))

    threshold = mandate.approval_threshold_minor
    if threshold is not None and intent.computed_total_minor > threshold:
        return Verdict(
            Decision.NEEDS_APPROVAL,
            (
                Reason(
                    Code.APPROVAL_REQUIRED,
                    f"Order total {intent.computed_total_minor} is above the approval threshold {threshold}.",
                ),
            ),
        )

    return Verdict(Decision.ALLOW)
