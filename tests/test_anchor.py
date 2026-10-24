import base64
import shutil
from pathlib import Path

import pytest

import flightrecorder.anchor as A
from conftest import T0, make_intent
from flightrecorder.anchor import AnchorCheck, AnchorError, anchor_ledger, build_request, imprint, verify_token
from flightrecorder.evidence import build_bundle, verify_bundle
from flightrecorder.ledger import Ledger
from flightrecorder.orchestrator import Orchestrator
from flightrecorder.paypal import PayPalClient, PayPalConfig
from test_orchestrator import FakePayPal

FIXTURE = Path(__file__).parent / "fixtures" / "digicert_head-hash-demo.tsr"


def test_request_is_well_formed_der():
    req = build_request(imprint("abc"))
    assert req[0] == 0x30 and req[1] == len(req) - 2 and len(req) == 59
    with pytest.raises(AnchorError):
        build_request(b"short")


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl not installed")
def test_real_digicert_token_verifies_only_for_its_own_hash():
    token = FIXTURE.read_bytes()
    assert verify_token("head-hash-demo", token).ok
    bad = verify_token("some-other-head", token)
    assert not bad.ok and "does not verify" in bad.detail


def test_garbage_token_never_verifies():
    assert not verify_token("h", b"not a timestamp").ok


def _world(signed, keypair):
    client = PayPalClient(PayPalConfig("id", "secret"), FakePayPal())
    orch = Orchestrator(Ledger(), client, trusted_public_key=keypair[1], clock=lambda: T0)
    orch.grant(signed)
    orch.submit(make_intent())
    return orch


def test_anchor_is_logged_only_after_the_token_checks_out(signed, keypair):
    orch = _world(signed, keypair)
    head = orch.ledger.head_hash()
    sent = {}
    res = anchor_ledger(orch.ledger, post=lambda url, body: sent.setdefault("body", body) and b"tok",
                        verify=lambda h, t: AnchorCheck(True, "Oct 1 2026 GMT", "ok"))
    ev = orch.ledger.events()[-1]
    assert ev.type == A.ANCHOR_RECORDED and ev.prev_hash == head == res["anchored_hash"]
    assert base64.b64decode(ev.payload["token_b64"]) == b"tok"
    assert sent["body"] == build_request(imprint(head))
    assert orch.ledger.verify().ok

    before = len(orch.ledger.events())
    with pytest.raises(AnchorError):
        anchor_ledger(orch.ledger, post=lambda u, b: b"bad", verify=lambda h, t: AnchorCheck(False, None, "nope"))
    assert len(orch.ledger.events()) == before          # a bad reply is never logged


def test_bundle_reports_anchors_and_events_after(signed, keypair, monkeypatch):
    orch = _world(signed, keypair)
    anchor_ledger(orch.ledger, post=lambda u, b: b"tok", verify=lambda h, t: AnchorCheck(True, "T1", "ok"))
    orch.submit(make_intent(merchant_id="evil.example"))        # 2 events after the anchor
    bundle = build_bundle(orch.ledger, signed)

    plain = verify_bundle(bundle)
    assert plain.ok and plain.anchors[0]["verified"] is None and plain.events_after_last_anchor == 2

    monkeypatch.setattr(A, "verify_token", lambda h, t: AnchorCheck(True, "T1", "ok"))
    assert verify_bundle(bundle, verify_anchors=True).anchors[0]["verified"] is True

    monkeypatch.setattr(A, "verify_token", lambda h, t: AnchorCheck(False, None, "token invalid"))
    bad = verify_bundle(bundle, verify_anchors=True)
    assert not bad.ok and any("timestamp anchor" in p for p in bad.problems)


def test_unanchored_bundle_says_everything_is_unpinned(signed, keypair):
    orch = _world(signed, keypair)
    check = verify_bundle(build_bundle(orch.ledger, signed))
    assert check.anchors == [] and check.events_after_last_anchor == len(orch.ledger.events())


def test_only_newest_anchors_are_checked_so_uploads_cannot_spawn_many_processes(signed, keypair, monkeypatch):
    orch = _world(signed, keypair)
    fake_ok = lambda h, t: AnchorCheck(True, "T", "ok")
    for _ in range(8):
        anchor_ledger(orch.ledger, post=lambda u, b: b"tok", verify=fake_ok)
    calls = []
    monkeypatch.setattr(A, "verify_token", lambda h, t: calls.append(h) or AnchorCheck(True, "T", "ok"))
    check = verify_bundle(build_bundle(orch.ledger, signed), verify_anchors=True)
    assert check.ok and len(calls) == 3
    assert [a["verified"] for a in check.anchors].count(None) == 5
