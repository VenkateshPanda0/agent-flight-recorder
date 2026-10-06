import dataclasses
from datetime import timedelta

from flightrecorder.guard import (
    Code,
    Decision,
    LineItem,
    MandateState,
    evaluate,
)
from flightrecorder.mandate import SignedMandate, generate_keypair, new_mandate, sign_mandate

from conftest import T0, make_intent

FRESH = MandateState()
NOW = T0 + timedelta(hours=1)


def test_in_scope_purchase_is_allowed(signed):
    verdict = evaluate(signed, make_intent(), FRESH, NOW)
    assert verdict.decision is Decision.ALLOW
    assert verdict.reasons == ()


def test_above_threshold_needs_human_approval(signed):
    intent = make_intent(items=(LineItem("Carbon racer", "footwear", 11_500, 1),))
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.NEEDS_APPROVAL
    assert verdict.codes == (Code.APPROVAL_REQUIRED,)


def test_exactly_at_threshold_does_not_need_approval(signed):
    intent = make_intent(items=(LineItem("Racer", "footwear", 10_000, 1),))
    assert evaluate(signed, intent, FRESH, NOW).decision is Decision.ALLOW


def test_over_per_order_cap_is_denied_not_sent_for_approval(signed):
    intent = make_intent(items=(LineItem("Gold shoes", "footwear", 12_001, 1),))
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.DENY
    assert Code.PER_ORDER_CAP_EXCEEDED in verdict.codes
    assert Code.APPROVAL_REQUIRED not in verdict.codes


def test_exactly_at_per_order_cap_is_not_denied(signed):
    intent = make_intent(items=(LineItem("Cap shoes", "footwear", 12_000, 1),))
    assert evaluate(signed, intent, FRESH, NOW).decision is Decision.NEEDS_APPROVAL


def test_wrong_merchant_is_denied(signed):
    verdict = evaluate(signed, make_intent(merchant_id="evil.example"), FRESH, NOW)
    assert verdict.decision is Decision.DENY
    assert verdict.codes == (Code.MERCHANT_NOT_ALLOWED,)


def test_merchant_match_ignores_case_and_spaces(signed):
    verdict = evaluate(signed, make_intent(merchant_id="  Shoes.Example "), FRESH, NOW)
    assert verdict.decision is Decision.ALLOW


def test_lookalike_merchant_is_denied(signed):
    for name in ("shoes.example.evil.io", "shoes-example", "xshoes.example"):
        verdict = evaluate(signed, make_intent(merchant_id=name), FRESH, NOW)
        assert Code.MERCHANT_NOT_ALLOWED in verdict.codes


def test_wrong_category_is_denied(signed):
    intent = make_intent(items=(LineItem("Gift card", "gift-cards", 5_000, 1),))
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.DENY
    assert verdict.codes == (Code.CATEGORY_NOT_ALLOWED,)


def test_one_bad_item_in_a_mixed_basket_denies_the_whole_order(signed):
    intent = make_intent(
        items=(
            LineItem("Trail runner", "footwear", 6_000, 1),
            LineItem("Gift card", "gift-cards", 2_000, 1),
        )
    )
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.DENY
    assert Code.CATEGORY_NOT_ALLOWED in verdict.codes


def test_wildcard_category_allows_any_category(keypair):
    private_key, _ = keypair
    mandate = new_mandate(
        principal_id="user-1",
        agent_id="agent-shopper",
        purpose="Anything from this shop",
        currency="USD",
        total_budget_minor=15_000,
        per_order_cap_minor=12_000,
        allowed_merchants=["shoes.example"],
        allowed_categories=["*"],
        created_at=T0,
        expires_at=T0 + timedelta(hours=24),
        mandate_id="m-1",
    )
    signed = sign_mandate(mandate, private_key)
    intent = make_intent(items=(LineItem("Gift card", "gift-cards", 5_000, 1),))
    assert evaluate(signed, intent, FRESH, NOW).decision is Decision.ALLOW


def test_under_reported_total_is_caught(signed):
    # The agent claims $50 but the items add up to $119.99 x 2.
    intent = make_intent(
        items=(LineItem("Racer", "footwear", 11_999, 2),), claimed_total_minor=5_000
    )
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.DENY
    assert Code.TOTAL_MISMATCH in verdict.codes
    assert Code.PER_ORDER_CAP_EXCEEDED in verdict.codes  # judged on the real total


def test_quantity_is_part_of_the_total(signed):
    intent = make_intent(items=(LineItem("Trail runner", "footwear", 8_999, 2),))
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert Code.PER_ORDER_CAP_EXCEEDED in verdict.codes


def test_negative_or_zero_amounts_are_denied(signed):
    for item in (
        LineItem("Refund trick", "footwear", -5_000, 1),
        LineItem("Free", "footwear", 0, 1),
        LineItem("Negative qty", "footwear", 5_000, -1),
        LineItem("Zero qty", "footwear", 5_000, 0),
    ):
        intent = make_intent(items=(item,))
        verdict = evaluate(signed, intent, FRESH, NOW)
        assert verdict.decision is Decision.DENY
        assert Code.INVALID_LINE_ITEM in verdict.codes


def test_negative_item_cannot_offset_an_expensive_one(signed):
    intent = make_intent(
        items=(
            LineItem("Expensive", "footwear", 30_000, 1),
            LineItem("Discount", "footwear", -25_000, 1),
        )
    )
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.DENY


def test_empty_order_is_denied(signed):
    intent = make_intent(items=(), claimed_total_minor=0)
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.codes == (Code.EMPTY_ORDER,)


def test_expired_mandate_is_denied(signed):
    verdict = evaluate(signed, make_intent(), FRESH, T0 + timedelta(hours=24))
    assert verdict.codes == (Code.MANDATE_EXPIRED,)
    verdict = evaluate(signed, make_intent(), FRESH, T0 + timedelta(days=30))
    assert verdict.codes == (Code.MANDATE_EXPIRED,)


def test_last_second_before_expiry_is_still_valid(signed):
    just_before = T0 + timedelta(hours=24) - timedelta(seconds=1)
    assert evaluate(signed, make_intent(), FRESH, just_before).decision is Decision.ALLOW


def test_mandate_not_yet_valid_is_denied(signed):
    verdict = evaluate(signed, make_intent(), FRESH, T0 - timedelta(seconds=1))
    assert verdict.codes == (Code.MANDATE_NOT_YET_VALID,)


def test_revoked_mandate_is_denied(signed):
    verdict = evaluate(signed, make_intent(), MandateState(revoked=True), NOW)
    assert verdict.codes == (Code.MANDATE_REVOKED,)


def test_different_agent_cannot_use_the_mandate(signed):
    verdict = evaluate(signed, make_intent(agent_id="agent-other"), FRESH, NOW)
    assert verdict.codes == (Code.AGENT_MISMATCH,)


def test_wrong_currency_is_denied(signed):
    verdict = evaluate(signed, make_intent(currency="EUR"), FRESH, NOW)
    assert verdict.codes == (Code.CURRENCY_MISMATCH,)


def test_intent_for_another_mandate_is_denied(signed):
    verdict = evaluate(signed, make_intent(mandate_id="m-2"), FRESH, NOW)
    assert verdict.codes == (Code.MANDATE_MISMATCH,)


def test_budget_already_used_is_denied(signed):
    state = MandateState(committed_minor=10_000, order_count=0)
    verdict = evaluate(signed, make_intent(), state, NOW)  # 8,999 > 5,000 left
    assert verdict.codes == (Code.BUDGET_EXCEEDED,)


def test_exactly_remaining_budget_is_allowed(keypair):
    private_key, _ = keypair
    mandate = new_mandate(
        principal_id="user-1",
        agent_id="agent-shopper",
        purpose="Several small orders",
        currency="USD",
        total_budget_minor=15_000,
        per_order_cap_minor=12_000,
        allowed_merchants=["shoes.example"],
        allowed_categories=["footwear"],
        max_orders=3,
        created_at=T0,
        expires_at=T0 + timedelta(hours=24),
        mandate_id="m-1",
    )
    signed = sign_mandate(mandate, private_key)
    state = MandateState(committed_minor=6_001, order_count=1)
    intent = make_intent(items=(LineItem("Runner", "footwear", 8_999, 1),))
    assert evaluate(signed, intent, state, NOW).decision is Decision.ALLOW
    one_cent_over = MandateState(committed_minor=6_002, order_count=1)
    assert evaluate(signed, intent, one_cent_over, NOW).codes == (Code.BUDGET_EXCEEDED,)


def test_order_count_limit_is_enforced(signed):
    state = MandateState(committed_minor=0, order_count=1)
    verdict = evaluate(signed, make_intent(), state, NOW)
    assert verdict.codes == (Code.MAX_ORDERS_EXCEEDED,)


def test_all_violations_are_reported_together(signed):
    intent = make_intent(
        merchant_id="evil.example",
        currency="EUR",
        agent_id="agent-other",
        items=(LineItem("Gift card", "gift-cards", 50_000, 1),),
    )
    verdict = evaluate(signed, intent, FRESH, NOW)
    assert verdict.decision is Decision.DENY
    assert set(verdict.codes) == {
        Code.AGENT_MISMATCH,
        Code.CURRENCY_MISMATCH,
        Code.MERCHANT_NOT_ALLOWED,
        Code.CATEGORY_NOT_ALLOWED,
        Code.PER_ORDER_CAP_EXCEEDED,
        Code.BUDGET_EXCEEDED,
    }


def test_tampered_mandate_is_denied_before_anything_else(signed):
    widened = SignedMandate(
        mandate=dataclasses.replace(signed.mandate, per_order_cap_minor=99_999_999),
        signature=signed.signature,
        public_key=signed.public_key,
    )
    intent = make_intent(items=(LineItem("Yacht shoes", "footwear", 5_000_000, 1),))
    verdict = evaluate(widened, intent, FRESH, NOW)
    assert verdict.codes == (Code.SIGNATURE_INVALID,)


def test_mandate_resigned_by_attacker_is_denied_with_trusted_key(signed, keypair):
    _, real_public_key = keypair
    attacker_key, _ = generate_keypair()
    forged = sign_mandate(
        dataclasses.replace(signed.mandate, allowed_merchants=("evil.example",)), attacker_key
    )
    verdict = evaluate(
        forged, make_intent(merchant_id="evil.example"), FRESH, NOW, real_public_key
    )
    assert verdict.codes == (Code.SIGNATURE_INVALID,)


def test_verdict_serialises_with_reason_codes(signed):
    verdict = evaluate(signed, make_intent(merchant_id="evil.example"), FRESH, NOW)
    assert verdict.to_dict() == {
        "decision": "DENY",
        "reasons": [
            {
                "code": "MERCHANT_NOT_ALLOWED",
                "message": "Merchant 'evil.example' is not on the mandate.",
            }
        ],
    }
