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
            assert headers["Prefer"] == "return=representation"
            return 201, {"id": "REF1", "status": "COMPLETED", "amount": payload["amount"]}
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


def test_provide_evidence_multipart_shape(fake):
    client = PayPalClient(PayPalConfig("id", "secret"), fake)
    orig = fake.__call__
    seen = {}

    def spy(method, url, headers, body):
        if "provide-evidence" in url:
            seen.update(headers=dict(headers), body=body)
            return 200, {}
        return orig(method, url, headers, body)

    client._transport = spy
    client.provide_evidence_with_document("PP-D-1", {"evidences": []}, "e.pdf", b"%PDF-1.4 demo")
    assert seen["headers"]["Content-Type"].startswith("multipart/form-data; boundary=")
    assert b'name="input"' in seen["body"] and b'filename="e.pdf"' in seen["body"]
    assert b"Content-Type: application/pdf" in seen["body"]


def test_failed_paypal_call_can_be_retried_with_same_request_id(orch, fake):
    fake.fail_orders = True
    assert orch.submit(make_intent()).status == "ORDER_FAILED"
    fake.fail_orders = False
    out = orch.submit(make_intent())
    assert out.status == "ORDER_CREATED"
    ids = {c[3].get("PayPal-Request-Id") for c in fake.order_posts()}
    assert len(ids) == 1 and None not in ids          # PayPal deduplicates on this
    assert orch.submit(make_intent()).status == "DUPLICATE"


def test_request_id_is_stable_within_a_mandate_but_differs_between_mandates(fake, signed, keypair):
    """Two sessions buying the same thing under different mandates must not share a PayPal-Request-Id,
    or PayPal would return the first session's order (with its stale log head)."""
    from datetime import timedelta
    from flightrecorder.mandate import new_mandate, sign_mandate
    ids = []
    for mandate_signed, mandate_id in ((signed, "m-1"), (None, "m-other")):
        if mandate_signed is None:
            m = new_mandate(principal_id="user-1", agent_id="agent-shopper", purpose="Buy one pair of running shoes under $120",
                            currency="USD", total_budget_minor=15_000, per_order_cap_minor=12_000, approval_threshold_minor=10_000,
                            allowed_merchants=["shoes.example"], allowed_categories=["footwear"], max_orders=1,
                            created_at=T0, expires_at=T0 + timedelta(hours=24), mandate_id=mandate_id)
            mandate_signed = sign_mandate(m, keypair[0])
        client = PayPalClient(PayPalConfig("id", "secret"), fake)
        o = Orchestrator(Ledger(), client, trusted_public_key=keypair[1], clock=lambda: T0)
        o.grant(mandate_signed)
        o.submit(make_intent(mandate_id=mandate_id))
        ids.append(fake.order_posts()[-1][3]["PayPal-Request-Id"])
    assert ids[0] != ids[1]

def test_captured_order_cannot_be_voided(orch):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    with pytest.raises(OrchestratorError):
        orch.void(order)
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999


def test_voided_order_cannot_be_captured(orch, fake):
    order = orch.submit(make_intent()).order_id
    orch.void(order)
    before = len(fake.calls)
    with pytest.raises(OrchestratorError):
        orch.capture(order)
    assert len(fake.calls) == before


def test_refund_replay_is_rejected_before_network(orch, fake):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    orch.refund(order)
    before = len(fake.calls)
    with pytest.raises(OrchestratorError):
        orch.refund(order)
    assert len(fake.calls) == before


def test_pending_refund_keeps_budget_reserved(orch, monkeypatch):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    monkeypatch.setattr(orch.paypal, 'refund_capture', lambda *a, **kw: {'id': 'R', 'status': 'PENDING'})
    with pytest.raises(OrchestratorError):
        orch.refund(order)
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999


def test_pending_refund_reconciliation_reads_existing_refund_and_releases_budget_once(orch, fake, monkeypatch):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    amount = {'currency_code': 'USD', 'value': '89.99'}
    requests = []

    def transport(method, url, headers, body):
        if '/v2/payments/' not in url:
            return fake(method, url, headers, body)
        requests.append((method, url))
        if method == 'POST':
            return 201, {'id': 'REF1', 'status': 'PENDING', 'amount': amount}
        assert url.endswith('/v2/payments/refunds/REF1')
        return 200, {'id': 'REF1', 'status': 'COMPLETED', 'amount': amount}

    monkeypatch.setattr(orch.paypal, '_transport', transport)
    with pytest.raises(OrchestratorError, match='not completed'):
        orch.refund(order)
    with pytest.raises(OrchestratorError, match='reconciled'):
        orch.refund(order)
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999
    assert orch.reconcile_refund(order)['refund_id'] == 'REF1'
    assert orch.reconcile_refund(order)['refund_id'] == 'REF1'
    assert [method for method, _ in requests] == ['POST', 'GET']
    assert orch.ledger.mandate_state('m-1').committed_minor == 0
    assert sum(e.type == L.ORDER_REFUNDED for e in orch.ledger.events()) == 1


def test_refund_timeout_blocks_retry_and_requires_proof_of_capture(orch, monkeypatch):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    calls = []

    def timeout(*args, **kwargs):
        calls.append('refund')
        raise PayPalError('timeout')

    monkeypatch.setattr(orch.paypal, 'refund_capture', timeout)
    with pytest.raises(PayPalError):
        orch.refund(order)
    with pytest.raises(OrchestratorError, match='reconciled'):
        orch.refund(order)
    assert calls == ['refund']

    with pytest.raises(OrchestratorError, match='refund ID'):
        orch.reconcile_refund(order)
    remote = {'id': 'REF1', 'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '89.99'}}
    monkeypatch.setattr(orch.paypal, 'get_refund', lambda _: remote)
    with pytest.raises(OrchestratorError, match='capture'):
        orch.reconcile_refund(order, 'REF1')
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999
    remote['links'] = [{'rel': 'up', 'href': 'https://api-m.sandbox.paypal.com/v2/payments/captures/CAP1'}]
    assert orch.reconcile_refund(order, 'REF1')['refund_id'] == 'REF1'
    assert orch.ledger.mandate_state('m-1').committed_minor == 0

def test_unhashable_proposal_still_exports_verifiable_evidence(orch, signed):
    from flightrecorder.evidence import build_bundle, verify_bundle
    orch.submit(make_intent(claimed_total_minor=89.99))
    assert verify_bundle(build_bundle(orch.ledger, signed)).ok

def test_different_signers_do_not_share_payment_request_ids(fake, mandate):
    from flightrecorder.mandate import generate_keypair, sign_mandate
    ids = []
    for _ in range(2):
        private, public = generate_keypair()
        orch = Orchestrator(Ledger(), PayPalClient(PayPalConfig('id', 'secret'), fake),
                            trusted_public_key=public, clock=lambda: T0)
        orch.grant(sign_mandate(mandate, private))
        orch.submit(make_intent())
        ids.append(fake.order_posts()[-1][3]['PayPal-Request-Id'])
    assert ids[0] != ids[1]

def test_evidence_upload_rejects_unsupported_or_unsafe_files(fake):
    client = PayPalClient(PayPalConfig('id', 'secret'), fake)
    for name, data in [('bundle.json', b'{}'), ('bad\r\n.pdf', b'%PDF'), ('large.pdf', b'x' * 10_000_000)]:
        with pytest.raises(PayPalError):
            client.provide_evidence_with_document('D', {}, name, data)
    assert fake.calls == []

def test_failed_approved_order_can_request_fresh_approval(orch, fake):
    intent = make_intent(items=(LineItem('Pro', 'footwear', 11000),))
    pending = orch.submit(intent)
    fake.fail_orders = True
    assert orch.approve(pending.intent_id, 'person').status == 'ORDER_FAILED'
    fake.fail_orders = False
    assert orch.submit(intent).status == 'PENDING_APPROVAL'
    before = len(orch.ledger.events())
    assert orch.submit(intent).status == 'PENDING_APPROVAL'
    assert len(orch.ledger.events()) == before
    assert orch.approve(pending.intent_id, 'person').status == 'ORDER_CREATED'


def test_rejected_retry_is_terminal_even_after_earlier_payment_failure(orch, fake):
    intent = make_intent(items=(LineItem('Pro', 'footwear', 11000),))
    pending = orch.submit(intent)
    fake.fail_orders = True
    orch.approve(pending.intent_id, 'person')
    fake.fail_orders = False
    orch.submit(intent)
    orch.reject(pending.intent_id, 'person')
    before = len(orch.ledger.events())
    replay = orch.submit(intent)
    assert replay.status == 'DENIED'
    assert replay.verdict.decision is Decision.DENY
    assert len(orch.ledger.events()) == before


def test_replaying_pending_intent_preserves_pending_status(orch):
    intent = make_intent(items=(LineItem('Pro', 'footwear', 11000),))
    assert orch.submit(intent).status == 'PENDING_APPROVAL'
    assert orch.submit(intent).status == 'PENDING_APPROVAL'


def test_orchestrators_sharing_ledger_cannot_overspend(orch, fake):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    other = Orchestrator(orch.ledger, orch.paypal, trusted_public_key=orch.trusted_public_key, clock=lambda: T0)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()
    create = orch.paypal.create_order
    def delayed(**kwargs):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return create(**kwargs)
    orch.paypal.create_order = delayed
    def second():
        second_started.set()
        return other.submit(make_intent(rationale='second shopper'))
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(orch.submit, make_intent())
        assert entered.wait(5)
        again = pool.submit(second)
        assert second_started.wait(5)
        try:
            # Old code permits the second purchase while the first is in flight.
            from concurrent.futures import TimeoutError
            try: again.result(timeout=0.1)
            except TimeoutError: pass
        finally:
            release.set()
        outcomes = [first.result(), again.result()]
    assert sorted(o.status for o in outcomes) == ['DENIED', 'ORDER_CREATED']
    assert len(fake.order_posts()) == 1


@pytest.mark.parametrize('changes', [
    {'merchant_id': None},
    {'items': (LineItem('Shoe', None, 8999),)},
    {'items': (LineItem(None, 'footwear', 8999),)},
    {'items': (LineItem('Shoe', 'footwear', '8999'),), 'claimed_total_minor': 8999},
    {'items': (LineItem('Shoe', 'footwear', 1),), 'claimed_total_minor': True},
])
def test_malformed_purchase_is_denied_and_evidence_still_verifies(orch, fake, signed, changes):
    from flightrecorder.evidence import build_bundle, verify_bundle
    from flightrecorder.guard import PurchaseIntent

    intent = PurchaseIntent(**{**vars(make_intent()), **changes})
    assert orch.submit(intent).status == 'DENIED'
    assert not fake.calls
    bundle = build_bundle(orch.ledger, signed)
    assert bundle['summary']['intents_denied'] == 1
    assert verify_bundle(bundle).ok


@pytest.mark.parametrize('response', [PayPalError('timed out'), PayPalError('server error', 500), {}])
def test_uncertain_order_retains_budget_and_blocks_replay(orch, monkeypatch, response):
    from flightrecorder.evidence import build_bundle, verify_bundle
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        if isinstance(response, PayPalError):
            raise response
        return response

    monkeypatch.setattr(orch.paypal, 'create_order', create)
    assert orch.submit(make_intent()).status == 'ORDER_UNCONFIRMED'
    state = orch.ledger.mandate_state('m-1')
    assert (state.committed_minor, state.order_count) == (8999, 1)
    assert orch.submit(make_intent()).status == 'ORDER_UNCONFIRMED'
    assert orch.submit(make_intent(rationale='another order')).status == 'DENIED'
    assert len(calls) == 1
    bundle = build_bundle(orch.ledger, orch._find_mandate('m-1'))
    assert any('outcome unknown' in line for line in bundle['summary']['plain_language'])
    assert verify_bundle(bundle).ok


def test_completed_capture_cannot_be_sent_again(orch, fake):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    before = len(fake.calls)
    with pytest.raises(OrchestratorError, match='already been captured'):
        orch.capture(order)
    assert len(fake.calls) == before


@pytest.mark.parametrize('condition, reason', [('revoked', 'MANDATE_REVOKED'), ('expired', 'MANDATE_EXPIRED')])
def test_capture_rechecks_mandate_before_sending_payment(orch, fake, signed, condition, reason):
    from datetime import timedelta
    from flightrecorder.evidence import build_bundle, verify_bundle

    order = orch.submit(make_intent()).order_id
    if condition == 'revoked':
        orch.revoke('m-1')
    else:
        orch._clock = lambda: T0 + timedelta(hours=24)
    before = len(fake.calls)
    with pytest.raises(OrchestratorError, match=condition):
        orch.capture(order)
    assert len(fake.calls) == before
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999
    blocked = orch.ledger.events(order_ref=order)[-1]
    assert blocked.type == L.CAPTURE_BLOCKED
    assert blocked.payload['reason_code'] == reason
    assert orch.ledger.verify().ok
    bundle = build_bundle(orch.ledger, signed)
    assert any(f'capture blocked ({reason})' in line for line in bundle['summary']['plain_language'])
    assert verify_bundle(bundle).ok


@pytest.mark.parametrize('response', [PayPalError('timeout'), {}])
def test_unconfirmed_capture_blocks_void_and_capture_replay(orch, monkeypatch, response):
    order = orch.submit(make_intent()).order_id
    calls = []

    def capture(*args, **kwargs):
        calls.append(order)
        if isinstance(response, PayPalError):
            raise response
        return response

    monkeypatch.setattr(orch.paypal, 'capture_order', capture)
    with pytest.raises((PayPalError, OrchestratorError)):
        orch.capture(order)
    with pytest.raises(OrchestratorError):
        orch.void(order)
    with pytest.raises(OrchestratorError):
        orch.capture(order)
    assert calls == [order]
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999


@pytest.mark.parametrize('amount', [None, {'currency_code': 'USD', 'value': '1.00'},
                                     {'currency_code': 'EUR', 'value': '89.99'}])
def test_inconsistent_refund_confirmation_keeps_budget(orch, monkeypatch, amount):
    order = orch.submit(make_intent()).order_id
    orch.capture(order)
    monkeypatch.setattr(orch.paypal, 'refund_capture', lambda *a, **kw: {
        'id': 'R', 'status': 'COMPLETED', 'amount': amount,
    })
    with pytest.raises(OrchestratorError):
        orch.refund(order)
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999


def test_definitive_capture_rejection_can_be_retried(orch, fake, monkeypatch):
    order = orch.submit(make_intent()).order_id

    def reject(method, url, headers, body):
        assert url.endswith('/capture')
        return 422, {'details': [{'issue': 'ORDER_NOT_APPROVED'}]}

    with monkeypatch.context() as patch:
        patch.setattr(orch.paypal, '_transport', reject)
        with pytest.raises(PayPalError):
            orch.capture(order)
    assert orch.capture(order)['purchase_units']


def test_mismatched_capture_amount_does_not_reduce_reservation(orch, monkeypatch):
    order = orch.submit(make_intent()).order_id
    monkeypatch.setattr(orch.paypal, 'capture_order', lambda *a, **kw: {
        'purchase_units': [{'payments': {'captures': [{
            'id': 'C', 'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': '1.00'},
        }]}}],
    })
    with pytest.raises(OrchestratorError, match='could not be confirmed'):
        orch.capture(order)
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999


@pytest.mark.parametrize('value', [89.99, '89.', '²'])
def test_parse_amount_rejects_malformed_decimal(value):
    with pytest.raises(PayPalError):
        parse_amount(value, 'USD')


def test_tampered_revocation_cannot_reenable_purchase(orch, fake):
    orch.revoke('m-1')
    with orch.ledger._db:
        orch.ledger._db.execute('UPDATE events SET type = ? WHERE type = ?', ('IGNORED', L.MANDATE_REVOKED))
    with pytest.raises(OrchestratorError, match='Ledger integrity check failed'):
        orch.submit(make_intent())
    assert not fake.calls


@pytest.mark.parametrize('action', ['approve', 'capture', 'refund', 'void'])
def test_tampered_ledger_blocks_payment_and_budget_changes(orch, fake, action):
    if action == 'approve':
        pending = orch.submit(make_intent(items=(LineItem('Pro', 'footwear', 11000),)))
        args = (pending.intent_id, 'person')
    else:
        order = orch.submit(make_intent()).order_id
        if action == 'refund':
            orch.capture(order)
        args = (order,)
    with orch.ledger._db:
        orch.ledger._db.execute("UPDATE events SET hash = 'tampered' WHERE seq = 1")
    before = len(fake.calls)
    events_before = len(orch.ledger.events())
    with pytest.raises(OrchestratorError, match='Ledger integrity check failed'):
        getattr(orch, action)(*args)
    assert len(fake.calls) == before
    assert len(orch.ledger.events()) == events_before


def test_capture_reconciliation_recovers_timeout_without_second_capture(orch, fake, monkeypatch):
    order = orch.submit(make_intent()).order_id
    captures, lookups = [], []

    def timeout(order_id, **kwargs):
        captures.append(order_id)
        raise PayPalError('timeout')

    state = {'id': 'CAP1', 'status': 'PENDING', 'amount': {'currency_code': 'USD', 'value': '89.99'}}

    def lookup(order_id):
        lookups.append(order_id)
        return {'id': order_id, 'purchase_units': [{'payments': {'captures': [dict(state)]}}]}

    monkeypatch.setattr(orch.paypal, 'capture_order', timeout)
    monkeypatch.setattr(orch.paypal, 'get_order', lookup)
    with pytest.raises(PayPalError):
        orch.capture(order)
    assert orch.reconcile_capture(order)['status'] == 'PENDING'
    before = orch.ledger.head_hash()
    assert orch.reconcile_capture(order)['status'] == 'PENDING'
    assert orch.ledger.head_hash() == before
    state.update(id='OTHER', status='COMPLETED')
    with pytest.raises(OrchestratorError, match='capture ID'):
        orch.reconcile_capture(order)
    assert orch.ledger.head_hash() == before
    state['id'] = 'CAP1'
    orch.revoke('m-1')  # Recording an earlier remote result is still allowed.
    completed = orch.reconcile_capture(order)
    assert completed['status'] == 'COMPLETED'
    assert orch.reconcile_capture(order) == completed
    assert captures == [order] and lookups == [order] * 4
    assert orch.ledger.mandate_state('m-1').committed_minor == 8999
    orch.refund(order)
    assert orch.ledger.mandate_state('m-1').committed_minor == 0


def test_capture_reconciliation_requires_matching_order_and_local_attempt(orch, monkeypatch):
    order = orch.submit(make_intent()).order_id
    lookups = []

    def lookup(order_id):
        lookups.append(order_id)
        return {'id': 'DIFFERENT', 'purchase_units': []}

    monkeypatch.setattr(orch.paypal, 'get_order', lookup)
    with pytest.raises(OrchestratorError, match='no unresolved capture'):
        orch.reconcile_capture(order)
    assert not lookups
    monkeypatch.setattr(orch.paypal, 'capture_order', lambda *a, **kw: {})
    with pytest.raises(OrchestratorError):
        orch.capture(order)
    before = orch.ledger.head_hash()
    with pytest.raises(OrchestratorError, match='order ID'):
        orch.reconcile_capture(order)
    assert lookups == [order]
    assert orch.ledger.head_hash() == before


def test_interrupted_order_request_still_reserves_budget_after_reopen(tmp_path, fake, signed, keypair, monkeypatch):
    path = str(tmp_path / 'orders.sqlite')
    client = PayPalClient(PayPalConfig('id', 'secret'), fake)
    first = Orchestrator(Ledger(path), client, trusted_public_key=keypair[1], clock=lambda: T0)
    first.grant(signed)

    def interrupted(**kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(client, 'create_order', interrupted)
        with pytest.raises(KeyboardInterrupt):
            first.submit(make_intent())
    first.ledger.close()
    reopened = Orchestrator(Ledger(path), client, trusted_public_key=keypair[1], clock=lambda: T0)
    try:
        assert reopened.ledger.mandate_state('m-1').committed_minor == 8999
        assert reopened.submit(make_intent()).status == 'ORDER_UNCONFIRMED'
        assert reopened.submit(make_intent(rationale='different purchase')).status == 'DENIED'
        assert not fake.calls
    finally:
        reopened.ledger.close()


def test_recover_unconfirmed_order_preserves_original_paypal_witness(orch, signed, monkeypatch):
    from flightrecorder.evidence import build_bundle, check_paypal_witness, verify_bundle

    requests = []
    remote = {'id': 'ORD1', 'status': 'APPROVED'}

    def timeout(**kwargs):
        requests.append('POST')
        remote['purchase_units'] = [{'description': kwargs['description'],
                                     'reference_id': kwargs['reference_id'],
                                     'amount': {'currency_code': 'USD', 'value': '89.99'}}]
        raise PayPalError('timeout')

    def lookup(order_id):
        assert order_id == 'ORD1'
        requests.append('GET')
        return remote

    monkeypatch.setattr(orch.paypal, 'create_order', timeout)
    monkeypatch.setattr(orch.paypal, 'get_order', lookup)
    out = orch.submit(make_intent())
    assert out.status == 'ORDER_UNCONFIRMED'
    before = orch.ledger.head_hash()
    correct = remote['purchase_units'][0]['description']
    remote['purchase_units'][0]['description'] = 'wrong checkpoint'
    with pytest.raises(OrchestratorError, match='does not match'):
        orch.reconcile_order(out.intent_id, 'ORD1')
    assert orch.ledger.head_hash() == before
    remote['purchase_units'][0]['description'] = correct
    remote['status'] = 'COMPLETED'
    with pytest.raises(OrchestratorError, match='unpaid'):
        orch.reconcile_order(out.intent_id, 'ORD1')
    remote['status'] = 'APPROVED'
    recovered = orch.reconcile_order(out.intent_id, 'ORD1')
    assert recovered.status == 'ORDER_CREATED' and recovered.order_id == 'ORD1'
    assert orch.reconcile_order(out.intent_id, 'ORD1').status == 'DUPLICATE'
    assert orch.submit(make_intent()).order_id == 'ORD1'
    assert requests == ['POST', 'GET', 'GET', 'GET']
    state = orch.ledger.mandate_state('m-1')
    assert (state.committed_minor, state.order_count) == (8999, 1)
    bundle = build_bundle(orch.ledger, signed)
    assert verify_bundle(bundle).ok
    witness, = check_paypal_witness(bundle, lookup)
    assert witness['logged_head_matches_chain'] and witness['matches_paypal'] is True
    assert witness['chain_head'] == correct.removeprefix('AFR log head ')
    assert witness['chain_head'] != before
    # Even a self-consistent rehash cannot point recovery at the wrong request.
    event = bundle['events'][-1]
    event['payload']['recovered_request_seq'] = 1
    event['hash'] = L.compute_event_hash(event['seq'], event['ts'], event['type'], event['mandate_id'],
                                         event['order_ref'], event['payload'], event['prev_hash'])
    bundle['head_hash'] = event['hash']
    assert any('original request' in p for p in verify_bundle(bundle).problems)
