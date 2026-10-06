import json

import pytest

from conftest import T0, make_intent
from flightrecorder import ledger as L
from flightrecorder.disputes import NOTES_LIMIT, DisputeError, DisputeHandler
from flightrecorder.evidence import verify_bundle
from flightrecorder.ledger import Ledger
from flightrecorder.orchestrator import Orchestrator
from flightrecorder.paypal import PayPalClient, PayPalConfig
from test_orchestrator import FakePayPal


class DisputePayPal(FakePayPal):
    def __init__(self):
        super().__init__()
        self.evidence = None

    def __call__(self, method, url, headers, body):
        path = url.split("paypal.com", 1)[1]
        if path.startswith("/v1/customer/disputes/PP-D-1") and method == "GET":
            self.calls.append((method, path, None, dict(headers)))
            return 200, {"dispute_id": "PP-D-1", "reason": "UNAUTHORISED", "status": "OPEN",
                         "disputed_transactions": [{"seller_transaction_id": "CAP1"}]}
        if path.endswith("provide-evidence"):
            self.calls.append((method, path, None, dict(headers)))
            self.evidence = body
            return 200, {}
        return super().__call__(method, url, headers, body)


@pytest.fixture
def setup(signed, keypair):
    fake = DisputePayPal()
    client = PayPalClient(PayPalConfig("id", "secret"), fake)
    orch = Orchestrator(Ledger(), client, trusted_public_key=keypair[1], clock=lambda: T0)
    orch.grant(signed)
    out = orch.submit(make_intent())
    orch.capture(out.order_id)
    return orch, DisputeHandler(orch.ledger, client, clock=lambda: T0), fake, signed


def test_dispute_is_matched_and_evidence_submitted(setup):
    orch, handler, fake, signed = setup
    result = handler.submit_evidence("PP-D-1", signed)
    types = [e.type for e in orch.ledger.events()]
    assert types.count(L.DISPUTE_OPENED) == 1 and types[-1] == L.EVIDENCE_SUBMITTED
    assert orch.ledger.verify().ok
    # The attached file is a verifiable bundle and matches the logged digest.
    start = fake.evidence.index(b"{", fake.evidence.index(b'filename='))
    end = fake.evidence.rindex(b"\r\n--afr")
    attached = json.loads(fake.evidence[start:end])
    assert verify_bundle(attached).ok
    assert result["bundle_digest"] in fake.evidence.decode()
    logged = orch.ledger.events()[-1].payload
    assert logged["bundle_head_hash"] == attached["head_hash"]


def test_notes_are_capped(setup):
    _, handler, fake, signed = setup
    handler.submit_evidence("PP-D-1", signed)
    notes = json.loads(fake.evidence.split(b"\r\n\r\n")[1].split(b"\r\n--")[0])["evidences"][0]["notes"]
    assert len(notes) <= NOTES_LIMIT


def test_unmatched_dispute_rejected(setup):
    orch, _, fake, signed = setup
    fake_other = DisputePayPal()
    client = PayPalClient(PayPalConfig("id", "secret"), fake_other)
    with pytest.raises(DisputeError):
        DisputeHandler(Ledger(), client).open("PP-D-1")


def test_opening_twice_logs_once(setup):
    orch, handler, _, _ = setup
    handler.open("PP-D-1"); handler.open("PP-D-1")
    assert sum(e.type == L.DISPUTE_OPENED for e in orch.ledger.events()) == 1
