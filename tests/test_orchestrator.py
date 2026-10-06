import json

import pytest

from conftest import T0, make_intent
from flightrecorder import ledger as L
from flightrecorder.guard import Decision, LineItem
from flightrecorder.ledger import Ledger
from flightrecorder.orchestrator import Orchestrator, OrchestratorError
from flightrecorder.paypal import (
    PayPalClient, PayPalConfig, PayPalError, format_amount, parse_amount,
)


class FakePayPal:
    def __init__(self):
        self.calls = []
        self.fail_orders = False

    def __call__(self, method, url, headers, body):
        path = url.split("paypal.com", 1)[1]
        payload = json.loads(body) if body and not path.startswith("/v1/oauth2") else None
        self.calls.append((method, path, payload, dict(headers)))
        if path == "/v1/oauth2/token":
            return 200, {"access_token": "tok", "expires_in": 3600}
        if path == "/v2/checkout/orders" and method == "POST":
            if self.fail_orders:
                return 422, {"name": "UNPROCESSABLE_ENTITY", "debug_id": "d1"}
            n = sum(1 for c in self.calls if c[1] == "/v2/checkout/orders")
            return 201, {"id": f"ORD{n}", "status": "CREATED",
                         "links": [{"rel": "approve", "href": f"https://pp/approve/ORD{n}"}]}
        if path.endswith("/capture"):
            amt = self._amount_for(path.split("/")[4])
            return 201, {"purchase_units": [{"payments": {"captures": [
                {"id": "CAP1", "status": "COMPLETED",
                 "amount": {"currency_code": "USD", "value": amt}}]}}]}
        if path.endswith("/refund"):
            return 201, {"id": "REF1"}
        return 404, {}

    def _amount_for(self, order_id):
        for m, p, pl, _ in self.calls:
            if p == "/v2/checkout/orders" and pl:
                return pl["purchase_units"][0]["amount"]["value"]
        return "0.00"

    def order_posts(self):
        return [c for c in self.calls if c[1] == "/v2/checkout/orders" and c[0] == "POST"]


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def orch(fake, signed, keypair):
    client = PayPalClient(PayPalConfig("id", "secret"), fake)
    o = Orchestrator(Ledger(), client, trusted_public_key=keypair[1], clock=lambda: T0)
    o.grant(signed)
    return o


def test_amount_formatting_has_no_floats():
    assert format_amount(8999, "USD") == "89.99"
    assert format_amount(5, "USD") == "0.05"
    assert format_amount(500, "JPY") == "500"
    assert parse_amount("89.99", "USD") == 8999
    assert parse_amount("90", "USD") == 9000
    with pytest.raises(PayPalError):
        format_amount(1, "XYZ")


def test_config_repr_hides_secrets():
    text = repr(PayPalConfig("my-id", "my-secret"))
    assert "my-id" not in text and "my-secret" not in text


def test_allowed_purchase_creates_order(orch, fake):
    out = orch.submit(make_intent())
    assert out.status == "ORDER_CREATED" and out.order_id == "ORD1"
    assert out.approve_url.endswith("ORD1")
    body = fake.order_posts()[0][2]
    assert body["purchase_units"][0]["amount"]["value"] == "89.99"
    assert body["purchase_units"][0]["custom_id"] == "m-1"
    assert orch.ledger.verify().ok
    assert orch.ledger.mandate_state("m-1").committed_minor == 8999


def test_denied_purchase_never_reaches_paypal(orch, fake):
    out = orch.submit(make_intent(merchant_id="evil.example"))
    assert out.status == "DENIED"
    assert fake.calls == []  # not even an OAuth call
    types = [e.type for e in orch.ledger.events()]
    assert L.INTENT_SUBMITTED in types and L.VERDICT_ISSUED in types
    assert L.ORDER_CREATED not in types


def test_unknown_mandate_denied(orch, fake):
    out = orch.submit(make_intent(mandate_id="nope"))
    assert out.status == "DENIED" and fake.calls == []


def test_replayed_intent_creates_one_order(orch, fake):
    first = orch.submit(make_intent())
    again = orch.submit(make_intent())
    assert again.status == "DUPLICATE" and again.order_id == first.order_id
    assert len(fake.order_posts()) == 1


def test_second_order_blocked_by_max_orders(orch, fake):
    orch.submit(make_intent())
    out = orch.submit(make_intent(rationale="again"))
    assert out.status == "DENIED"
    assert len(fake.order_posts()) == 1


def test_approval_flow(orch, fake):
    big = make_intent(items=(LineItem("Pro shoe", "footwear", 11_000, 1),))
    out = orch.submit(big)
    assert out.status == "PENDING_APPROVAL" and fake.calls == []
    done = orch.approve(out.intent_id, "user-1")
    assert done.status == "ORDER_CREATED" and len(fake.order_posts()) == 1
    with pytest.raises(OrchestratorError):
        orch.approve(out.intent_id, "user-1")


def test_approval_cannot_override_revocation(orch, fake):
    big = make_intent(items=(LineItem("Pro shoe", "footwear", 11_000, 1),))
    out = orch.submit(big)
    orch.revoke("m-1", "changed my mind")
    done = orch.approve(out.intent_id, "user-1")
    assert done.status == "DENIED" and fake.calls == []


def test_rejected_approval_cannot_be_approved_later(orch):
    out = orch.submit(make_intent(items=(LineItem("Pro shoe", "footwear", 11_000, 1),)))
    orch.reject(out.intent_id, "user-1")
    with pytest.raises(OrchestratorError):
        orch.approve(out.intent_id, "user-1")


def test_paypal_failure_is_logged_and_frees_budget(orch, fake):
    fake.fail_orders = True
    out = orch.submit(make_intent())
    assert out.status == "ORDER_FAILED"
    assert orch.ledger.mandate_state("m-1").committed_minor == 0
    assert any(e.type == L.ORDER_FAILED for e in orch.ledger.events())


def test_capture_void_refund_update_state(orch):
    out = orch.submit(make_intent())
    orch.capture(out.order_id)
    assert orch.ledger.mandate_state("m-1").committed_minor == 8999
    orch.refund(out.order_id)
    assert orch.ledger.mandate_state("m-1").committed_minor == 0


def test_void_releases_slot(orch):
    out = orch.submit(make_intent())
    orch.void(out.order_id)
    assert orch.ledger.mandate_state("m-1").order_count == 0
    assert orch.submit(make_intent(rationale="retry")).status == "ORDER_CREATED"


def test_token_is_cached(orch, fake):
    orch.submit(make_intent())
    orch.void("ORD1")
    orch.submit(make_intent(rationale="x"))
    assert sum(1 for c in fake.calls if c[1] == "/v1/oauth2/token") == 1
