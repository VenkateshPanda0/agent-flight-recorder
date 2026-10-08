import copy
import json

import pytest

from conftest import T0, make_intent
from flightrecorder.evidence import build_bundle, verify_bundle
from flightrecorder.guard import LineItem
from flightrecorder.ledger import Ledger
from flightrecorder.mandate import generate_keypair
from flightrecorder.orchestrator import Orchestrator
from flightrecorder.paypal import PayPalClient, PayPalConfig
from test_orchestrator import FakePayPal


@pytest.fixture
def world(signed, keypair):
    client = PayPalClient(PayPalConfig("id", "secret"), FakePayPal())
    orch = Orchestrator(Ledger(), client, trusted_public_key=keypair[1], clock=lambda: T0)
    orch.grant(signed)
    orch.submit(make_intent(merchant_id="evil.example"))      # denied
    out = orch.submit(make_intent())                           # allowed
    orch.capture(out.order_id)
    return orch, signed, keypair[1]


@pytest.fixture
def bundle(world):
    orch, signed, _ = world
    # Round-trip through JSON: a bundle is a file that gets edited, not an object.
    return json.loads(json.dumps(build_bundle(orch.ledger, signed)))


def test_clean_bundle_verifies(bundle, world):
    _, _, pub = world
    check = verify_bundle(bundle, trusted_public_key=pub, expected_head_hash=bundle["head_hash"])
    assert check.ok, check.problems
    assert check.signature_ok and check.chain_ok


def test_summary_says_allowed_and_happened(bundle):
    text = " ".join(bundle["summary"]["plain_language"])
    assert "shoes.example" in text and "MERCHANT_NOT_ALLOWED" in text
    assert "captured" in text
    assert bundle["summary"]["orders_created"] == 1
    assert bundle["summary"]["intents_denied"] == 1


def _tamper(bundle, fn):
    b = copy.deepcopy(bundle)
    fn(b)
    return b


def test_edited_event_payload_detected(bundle):
    def edit(b):
        for e in b["events"]:
            if e["type"] == "ORDER_CREATED":
                e["payload"]["amount_minor"] = 1
    check = verify_bundle(_tamper(bundle, edit))
    assert not check.ok and check.first_bad_seq is not None
    assert "hash" in " ".join(check.problems)


def test_reordered_events_detected(bundle):
    check = verify_bundle(_tamper(bundle, lambda b: b["events"].reverse()))
    assert not check.ok


def test_removed_middle_event_detected(bundle):
    check = verify_bundle(_tamper(bundle, lambda b: b["events"].pop(2)))
    assert not check.ok


def test_edited_mandate_detected(bundle):
    def edit(b):
        b["signed_mandate"]["mandate"]["per_order_cap_minor"] = 999_999
    check = verify_bundle(_tamper(bundle, edit))
    assert not check.ok and not check.signature_ok


def test_resigned_mandate_with_forger_key_detected(bundle, world):
    """A forger signs an edited mandate with their own key and fixes the log."""
    from flightrecorder.mandate import Mandate, sign_mandate
    _, signed, pub = world
    forger, _ = generate_keypair()
    payload = signed.mandate.to_payload()
    payload["per_order_cap_minor"] = 999_999
    forged = sign_mandate(Mandate.from_payload(payload), forger).to_dict()
    b = _tamper(bundle, lambda b: b.update(signed_mandate=forged))
    # Without a trusted key the signature itself is valid, but the log differs.
    assert not verify_bundle(b).ok
    # With the person's real key it is rejected outright.
    check = verify_bundle(b, trusted_public_key=pub)
    assert not check.ok and not check.signature_ok


def test_wrong_trusted_key_detected(bundle):
    _, other = generate_keypair()
    assert not verify_bundle(bundle, trusted_public_key=other).ok


def test_truncated_tail_only_caught_with_saved_head(bundle):
    def cut(b):
        b["events"].pop()
        b["head_hash"] = b["events"][-1]["hash"]
    truncated = _tamper(bundle, cut)
    # Honest limit: on its own the shortened chain is self-consistent...
    assert verify_bundle(truncated).chain_ok
    # ...but the summary changes and the saved head hash exposes it.
    check = verify_bundle(truncated, expected_head_hash=bundle["head_hash"])
    assert not check.ok and "saved" in " ".join(check.problems)


def test_edited_summary_detected(bundle):
    def edit(b):
        b["summary"]["plain_language"][0] = "The agent was allowed to buy anything."
    assert not verify_bundle(_tamper(bundle, edit)).ok


def test_garbage_does_not_raise():
    assert not verify_bundle({}).ok
    assert not verify_bundle({"signed_mandate": 5}).ok


def test_cli_verifier_exit_codes(tmp_path, bundle, world):
    import subprocess, sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / "scripts" / "verify_bundle.py"
    good = tmp_path / "good.json"; good.write_text(json.dumps(bundle))
    bad_b = copy.deepcopy(bundle); bad_b["events"][3]["payload"]["x"] = 1
    bad = tmp_path / "bad.json"; bad.write_text(json.dumps(bad_b))
    pub = world[2]
    ok = subprocess.run([sys.executable, str(script), str(good), "--key", pub, "--head", bundle["head_hash"]],
                        capture_output=True, text=True)
    assert ok.returncode == 0 and "bundle verifies" in ok.stdout
    no = subprocess.run([sys.executable, str(script), str(bad)], capture_output=True, text=True)
    assert no.returncode == 1 and "FAILED" in no.stdout
