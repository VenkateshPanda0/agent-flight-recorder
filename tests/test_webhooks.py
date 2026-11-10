from conftest import T0, make_intent
from flightrecorder import ledger as L
from flightrecorder.ledger import Ledger
from flightrecorder.orchestrator import Orchestrator
from flightrecorder.paypal import PayPalClient, PayPalConfig
from flightrecorder.webhooks import WebhookProcessor
from flightrecorder.disputes import DisputeHandler
from test_disputes import DisputePayPal

HEADERS = {"PayPal-Auth-Algo": "a", "PayPal-Cert-Url": "u", "PayPal-Transmission-Id": "t",
           "PayPal-Transmission-Sig": "s", "PayPal-Transmission-Time": "x"}


class Verifying(DisputePayPal):
    status = "SUCCESS"

    def __call__(self, method, url, headers, body):
        if url.endswith("verify-webhook-signature"):
            return 200, {"verification_status": self.status}
        return super().__call__(method, url, headers, body)


def make(signed, keypair):
    fake = Verifying()
    client = PayPalClient(PayPalConfig("id", "secret"), fake)
    orch = Orchestrator(Ledger(), client, trusted_public_key=keypair[1], clock=lambda: T0)
    orch.grant(signed)
    out = orch.submit(make_intent())
    orch.capture(out.order_id)
    wp = WebhookProcessor(orch.ledger, client, "WH-1", DisputeHandler(orch.ledger, client))
    return orch, wp, fake


def test_valid_webhook_logged_once(signed, keypair):
    orch, wp, _ = make(signed, keypair)
    event = {"id": "WH-EV-1", "event_type": "PAYMENT.CAPTURE.COMPLETED",
             "resource": {"id": "CAP1", "supplementary_data": {"related_ids": {"order_id": "ORD1"}}}}
    assert wp.handle(HEADERS, event) == (200, "ok")
    assert wp.handle(HEADERS, event) == (200, "duplicate")
    logged = [e for e in orch.ledger.events() if e.type == L.WEBHOOK_RECEIVED]
    assert len(logged) == 1 and logged[0].mandate_id == "m-1"


def test_bad_signature_rejected_and_not_logged(signed, keypair):
    orch, wp, fake = make(signed, keypair)
    fake.status = "FAILURE"
    before = len(orch.ledger.events())
    assert wp.handle(HEADERS, {"id": "x", "event_type": "T"})[0] == 401
    assert len(orch.ledger.events()) == before


def test_missing_headers_rejected(signed, keypair):
    _, wp, _ = make(signed, keypair)
    assert wp.handle({}, {"id": "x", "event_type": "T"})[0] == 401


def test_dispute_created_opens_dispute(signed, keypair):
    orch, wp, _ = make(signed, keypair)
    wp.handle(HEADERS, {"id": "WH-EV-2", "event_type": "CUSTOMER.DISPUTE.CREATED",
                        "resource": {"dispute_id": "PP-D-1"}})
    assert any(e.type == L.DISPUTE_OPENED for e in orch.ledger.events())

def test_concurrent_webhooks_are_logged_once(signed, keypair, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    orch, wp, _ = make(signed, keypair)
    original = orch.ledger.append
    entered, release = threading.Event(), threading.Event()
    def paused_append(*args, **kwargs):
        if args[0] == L.WEBHOOK_RECEIVED:
            entered.set()
            assert release.wait(3)
        return original(*args, **kwargs)
    monkeypatch.setattr(orch.ledger, 'append', paused_append)
    event = {'id': 'race', 'event_type': 'TEST'}
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(wp.handle, HEADERS, event)
        assert entered.wait(3)
        second = pool.submit(wp.handle, HEADERS, event)
        # Give the second invocation an opportunity to reach the deduplication check.
        import time
        time.sleep(0.05)
        release.set()
        results = [first.result(), second.result()]
    assert sorted(r[1] for r in results) == ['duplicate', 'ok']
    assert sum(e.type == L.WEBHOOK_RECEIVED for e in orch.ledger.events()) == 1

def test_failed_dispute_fetch_is_retried_without_duplicate_receipt(signed, keypair, monkeypatch):
    from flightrecorder.paypal import PayPalError
    orch, processor, _ = make(signed, keypair)
    calls = []
    original = processor.disputes.open
    def flaky(dispute_id):
        calls.append(dispute_id)
        if len(calls) == 1:
            raise PayPalError('temporary network failure')
        return original(dispute_id)
    monkeypatch.setattr(processor.disputes, 'open', flaky)
    event = {'id': 'retry-dispute', 'event_type': 'CUSTOMER.DISPUTE.CREATED',
             'resource': {'dispute_id': 'PP-D-1'}}
    assert processor.handle(HEADERS, event)[0] == 503
    processor = WebhookProcessor(orch.ledger, processor.paypal, processor.webhook_id, processor.disputes)
    assert processor.handle(HEADERS, event) == (200, 'ok')
    assert processor.handle(HEADERS, event) == (200, 'duplicate')
    assert len(calls) == 2
    assert sum(e.type == L.WEBHOOK_RECEIVED for e in orch.ledger.events()) == 1
    assert sum(e.type == L.DISPUTE_OPENED for e in orch.ledger.events()) == 1

def test_completed_webhook_is_deduplicated_after_processor_restart(signed, keypair):
    orch, processor, _ = make(signed, keypair)
    event = {'id': 'restart', 'event_type': 'TEST'}
    assert processor.handle(HEADERS, event) == (200, 'ok')
    restarted = WebhookProcessor(orch.ledger, processor.paypal, processor.webhook_id, processor.disputes)
    assert restarted.handle(HEADERS, event) == (200, 'duplicate')
