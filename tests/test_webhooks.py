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
