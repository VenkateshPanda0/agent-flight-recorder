from datetime import datetime, timedelta, timezone

import pytest

from flightrecorder.guard import LineItem, PurchaseIntent
from flightrecorder.mandate import generate_keypair, new_mandate, sign_mandate

T0 = datetime(2026, 10, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def keypair():
    return generate_keypair()


@pytest.fixture
def mandate():
    """Buy running shoes from one shop: $150 budget, $120 per order, one order."""
    return new_mandate(
        principal_id="user-1",
        agent_id="agent-shopper",
        purpose="Buy one pair of running shoes under $120",
        currency="USD",
        total_budget_minor=15_000,
        per_order_cap_minor=12_000,
        approval_threshold_minor=10_000,
        allowed_merchants=["shoes.example"],
        allowed_categories=["footwear"],
        max_orders=1,
        created_at=T0,
        expires_at=T0 + timedelta(hours=24),
        mandate_id="m-1",
    )


@pytest.fixture
def signed(mandate, keypair):
    private_key, _ = keypair
    return sign_mandate(mandate, private_key)


def make_intent(**overrides) -> PurchaseIntent:
    items = overrides.pop("items", (LineItem("Trail runner", "footwear", 8_999, 1),))
    total = sum(i.unit_price_minor * i.quantity for i in items)
    base = dict(
        mandate_id="m-1",
        agent_id="agent-shopper",
        merchant_id="shoes.example",
        currency="USD",
        items=tuple(items),
        claimed_total_minor=total,
        rationale="Cheapest in-stock pair matching the request.",
    )
    base.update(overrides)
    return PurchaseIntent(**base)
