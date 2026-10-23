"""Randomised checks. Seeded, so failures reproduce."""
import random
from datetime import timedelta

from conftest import T0
from flightrecorder.guard import (Code, Decision, LineItem, MandateState, PurchaseIntent,
                                  evaluate)
from flightrecorder.mandate import generate_keypair, new_mandate, sign_mandate
from flightrecorder.redteam import tamper_sweep

CASES = 5_000


def test_every_single_edit_to_a_bundle_is_detected():
    result = tamper_sweep()
    assert result["total"] >= 150
    assert result["missed"] == []


def _oracle(m, it, st, now):
    """Independent restatement of the rules as plain booleans."""
    hard = set()
    if it.mandate_id != m.mandate_id: hard.add(Code.MANDATE_MISMATCH)
    if st.revoked: hard.add(Code.MANDATE_REVOKED)
    if now >= T0 + timedelta(hours=24): hard.add(Code.MANDATE_EXPIRED)
    if it.agent_id != m.agent_id: hard.add(Code.AGENT_MISMATCH)
    if it.currency != m.currency: hard.add(Code.CURRENCY_MISMATCH)
    if it.merchant_id.strip().lower() not in m.allowed_merchants: hard.add(Code.MERCHANT_NOT_ALLOWED)
    if not it.items: hard.add(Code.EMPTY_ORDER)
    valid = True
    for i in it.items:
        if not (type(i.unit_price_minor) is int and i.unit_price_minor > 0
                and type(i.quantity) is int and i.quantity > 0):
            valid = False; hard.add(Code.INVALID_LINE_ITEM)
        elif i.category.strip().lower() not in m.allowed_categories:
            hard.add(Code.CATEGORY_NOT_ALLOWED)
    if valid and it.items:
        total = sum(i.unit_price_minor * i.quantity for i in it.items)
        if it.claimed_total_minor != total: hard.add(Code.TOTAL_MISMATCH)
        if total > m.per_order_cap_minor: hard.add(Code.PER_ORDER_CAP_EXCEEDED)
        if total > m.total_budget_minor - st.committed_minor: hard.add(Code.BUDGET_EXCEEDED)
    if st.order_count >= m.max_orders: hard.add(Code.MAX_ORDERS_EXCEEDED)
    if hard:
        return Decision.DENY, hard
    total = sum(i.unit_price_minor * i.quantity for i in it.items)
    if m.approval_threshold_minor is not None and total > m.approval_threshold_minor:
        return Decision.NEEDS_APPROVAL, {Code.APPROVAL_REQUIRED}
    return Decision.ALLOW, set()


def test_guard_matches_independent_oracle_on_random_cases():
    rng = random.Random(20261023)
    key, pub = generate_keypair()
    ok_merchants = ["shoes.example", "books.example"]
    bad_merchants = ["evil.example", "shoes.example.evil.com", "sh0es.example", "shоes.example", ""]
    allowed = {"decisions": {Decision.ALLOW: 0, Decision.NEEDS_APPROVAL: 0, Decision.DENY: 0}}
    for n in range(CASES):
        m = new_mandate(
            principal_id="u", agent_id="a", purpose="p", currency="USD",
            total_budget_minor=(budget := rng.randint(10_000, 50_000)),
            per_order_cap_minor=rng.randint(9_000, budget),
            approval_threshold_minor=rng.choice([None, rng.randint(0, 20_000)]),
            allowed_merchants=ok_merchants[: rng.randint(1, 2)],
            allowed_categories=rng.choice([["footwear"], ["footwear", "books"]]),
            max_orders=rng.randint(1, 4), created_at=T0, expires_at=T0 + timedelta(hours=24),
            mandate_id="m",
        )
        signed = sign_mandate(m, key)
        def bad(p=0.06):
            return rng.random() < p

        items = tuple(
            LineItem(f"i{k}",
                     rng.choice(["giftcards", "books"]) if bad() else rng.choice(["footwear", " Footwear "]),
                     rng.choice([0, -5, True]) if bad(0.03) else rng.randint(500, 9_000),
                     rng.choice([0, -1, False, 10**6]) if bad(0.03) else rng.choice([1, 1, 1, 2]))
            for k in range(0 if bad(0.03) else rng.choice([1, 1, 2])))
        true_total = sum(i.unit_price_minor * i.quantity for i in items)
        it = PurchaseIntent(
            mandate_id="x" if bad() else "m", agent_id="b" if bad() else "a",
            merchant_id=rng.choice(bad_merchants) if bad() else rng.choice(ok_merchants[:1]),
            currency="EUR" if bad() else "USD", items=items,
            claimed_total_minor=true_total + 1 if bad() else true_total)
        st = MandateState(committed_minor=rng.randint(0, budget // 2), order_count=rng.randint(0, 2),
                          revoked=bad(0.03))
        now = T0 + timedelta(hours=rng.choice([1, 2, 5, 23, 24 if bad(0.5) else 3]))
        v = evaluate(signed, it, st, now, pub)
        want, codes = _oracle(m, it, st, now)
        assert v.decision == want, (n, v, want, codes)
        assert set(v.codes) == codes, (n, v.codes, codes)
        allowed["decisions"][v.decision] += 1
    # The generator must actually exercise every outcome, or the test proves nothing.
    assert all(count > 50 for count in allowed["decisions"].values()), allowed
