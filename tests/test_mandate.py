import dataclasses
from datetime import timedelta

import pytest

from flightrecorder.canonical import canonical_json, digest
from flightrecorder.mandate import (
    MandateError,
    SignedMandate,
    generate_keypair,
    new_mandate,
    sign_mandate,
    verify_mandate,
)

from conftest import T0


def base_kwargs(**overrides):
    kwargs = dict(
        principal_id="user-1",
        agent_id="agent-shopper",
        purpose="Buy shoes",
        currency="USD",
        total_budget_minor=15_000,
        per_order_cap_minor=12_000,
        allowed_merchants=["shoes.example"],
        allowed_categories=["footwear"],
        created_at=T0,
        expires_at=T0 + timedelta(hours=1),
    )
    kwargs.update(overrides)
    return kwargs


def test_signed_mandate_verifies(signed, keypair):
    _, public_key = keypair
    assert verify_mandate(signed)
    assert verify_mandate(signed, trusted_public_key=public_key)


@pytest.mark.parametrize(
    "field,value",
    [
        ("total_budget_minor", 999_999),
        ("per_order_cap_minor", 14_000),
        ("allowed_merchants", ("evil.example",)),
        ("allowed_categories", ("*",)),
        ("expires_at", "2030-01-01T00:00:00Z"),
        ("agent_id", "other-agent"),
        ("max_orders", 50),
        ("approval_threshold_minor", None),
    ],
)
def test_editing_any_field_breaks_the_signature(signed, field, value):
    tampered = SignedMandate(
        mandate=dataclasses.replace(signed.mandate, **{field: value}),
        signature=signed.signature,
        public_key=signed.public_key,
    )
    assert not verify_mandate(tampered)


def test_resigning_with_another_key_fails_against_trusted_key(signed, keypair):
    _, real_public_key = keypair
    attacker_key, _ = generate_keypair()
    widened = dataclasses.replace(signed.mandate, total_budget_minor=999_999)
    forged = sign_mandate(widened, attacker_key)
    assert verify_mandate(forged)  # self-consistent...
    assert not verify_mandate(forged, trusted_public_key=real_public_key)  # ...but not trusted


def test_garbage_signature_is_rejected_not_raised(signed):
    for bad in ("", "zz", "00" * 64):
        broken = SignedMandate(signed.mandate, bad, signed.public_key)
        assert not verify_mandate(broken)
    assert not verify_mandate(SignedMandate(signed.mandate, signed.signature, "nothex"))


def test_round_trip_through_dict_keeps_signature_valid(signed):
    restored = SignedMandate.from_dict(signed.to_dict())
    assert restored == signed
    assert verify_mandate(restored)


def test_merchants_and_categories_are_normalised():
    m = new_mandate(
        **base_kwargs(
            allowed_merchants=[" Shoes.Example ", "shoes.example", "B.example"],
            allowed_categories=["Footwear", " SOCKS"],
        )
    )
    assert m.allowed_merchants == ("b.example", "shoes.example")
    assert m.allowed_categories == ("footwear", "socks")


@pytest.mark.parametrize(
    "overrides",
    [
        dict(currency="usd"),
        dict(currency="DOLLARS"),
        dict(total_budget_minor=0),
        dict(total_budget_minor=-5),
        dict(total_budget_minor=150.0),
        dict(total_budget_minor=True),
        dict(per_order_cap_minor=20_000),  # above total budget
        dict(max_orders=0),
        dict(approval_threshold_minor=-1),
        dict(allowed_merchants=[]),
        dict(allowed_merchants=["  "]),
        dict(allowed_merchants=["*"]),  # merchants can never be a wildcard
        dict(allowed_categories=[]),
        dict(expires_at=T0),  # not after created_at
        dict(expires_at=T0 - timedelta(seconds=1)),
        dict(principal_id=" "),
        dict(agent_id=""),
    ],
)
def test_malformed_mandates_are_refused(overrides):
    with pytest.raises(MandateError):
        new_mandate(**base_kwargs(**overrides))


def test_naive_datetimes_are_refused():
    with pytest.raises(MandateError):
        new_mandate(**base_kwargs(expires_at=(T0 + timedelta(hours=1)).replace(tzinfo=None)))


def test_canonical_json_is_order_independent_and_rejects_floats():
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})
    assert digest({"a": 1}) != digest({"a": 2})
    with pytest.raises(TypeError):
        canonical_json({"amount": 1.5})
    with pytest.raises(TypeError):
        canonical_json({"nested": [{"amount": 0.1}]})
