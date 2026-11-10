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


def test_verifier_rejects_non_object_payload_in_an_unrelated_mandate(bundle):
    from flightrecorder.ledger import compute_event_hash

    event = {'seq': len(bundle['events']) + 1, 'ts': '2026-10-06T12:00:00Z',
             'type': 'TEST', 'mandate_id': 'unrelated', 'order_ref': None,
             'payload': [], 'prev_hash': bundle['head_hash']}
    event['hash'] = compute_event_hash(event['seq'], event['ts'], event['type'], event['mandate_id'],
                                      event['order_ref'], event['payload'], event['prev_hash'])
    bundle['events'].append(event)
    bundle['head_hash'] = event['hash']
    check = verify_bundle(bundle)
    assert not check.ok and check.first_bad_seq == event['seq']


@pytest.mark.parametrize('edit', [
    lambda b: b.update(bundle_version=True),
    lambda b: b['summary'].update(orders_created=True),
    lambda b: b['summary'].update(orders_created=1.0),
    lambda b: b['events'][0].update(note='unhashed assertion'),
    lambda b: b.update(note='unverified assertion'),
])
def test_saved_head_does_not_make_unchecked_edits_acceptable(bundle, edit):
    head = bundle['head_hash']
    edit(bundle)
    assert not verify_bundle(bundle, expected_head_hash=head).ok


def test_cli_rejects_duplicate_bundle_fields(bundle, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    path = tmp_path / 'duplicate.json'
    path.write_text('{"bundle_version":99,' + json.dumps(bundle)[1:], encoding='utf-8')
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_bundle.py'
    result = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert 'duplicate JSON field' in result.stderr


def test_summary_says_allowed_and_happened(bundle):
    text = " ".join(bundle["summary"]["plain_language"])
    assert "shoes.example" in text and "MERCHANT_NOT_ALLOWED" in text
    assert "captured" in text
    assert bundle["summary"]["orders_created"] == 1
    assert bundle["summary"]["intents_denied"] == 1


def test_pending_capture_is_distinct_from_completed_in_exported_evidence(world, monkeypatch):
    from flightrecorder import ledger as L

    orch, signed, _ = world
    # Use a fresh order because the fixture's first order is already completed.
    ledger = Ledger()
    current = Orchestrator(ledger, orch.paypal, clock=lambda: T0)
    current.grant(signed)
    order_id = current.submit(make_intent()).order_id
    payment = {'id': 'CAP2', 'status': 'PENDING', 'amount': {'currency_code': 'USD', 'value': '89.99'}}
    monkeypatch.setattr(current.paypal, 'capture_order', lambda *a, **kw: {
        'purchase_units': [{'payments': {'captures': [payment]}}],
    })
    current.capture(order_id)
    bundle = build_bundle(ledger, signed)
    assert bundle['summary']['intents'][0]['captured'] is False
    assert any('capture pending' in line for line in bundle['summary']['plain_language'])
    assert verify_bundle(bundle).ok
    ledger.append(L.ORDER_CAPTURED, {'amount_minor': 8999, 'currency': 'USD',
                                     'capture_id': 'CAP2', 'status': 'COMPLETED'},
                  mandate_id=signed.mandate.mandate_id, order_ref=order_id)
    assert build_bundle(ledger, signed)['summary']['intents'][0]['captured'] is True


def test_evidence_formats_zero_decimal_currency_and_keeps_legacy_bundles_verifiable(signed, keypair):
    from dataclasses import replace
    from flightrecorder.evidence import summarise
    from flightrecorder.mandate import sign_mandate

    signed = sign_mandate(replace(signed.mandate, currency='JPY'), keypair[0])
    ledger = Ledger()
    Orchestrator(ledger, PayPalClient(PayPalConfig('id', 'fixture'), FakePayPal())).grant(signed)
    bundle = build_bundle(ledger, signed)
    assert f'{signed.mandate.total_budget_minor} JPY' in bundle['summary']['plain_language'][0]
    assert verify_bundle(bundle).ok
    bundle['bundle_version'] = 1
    bundle['summary'] = summarise(signed.mandate.to_payload(), bundle['events'], version=1)
    assert f'{signed.mandate.total_budget_minor // 100}.00 JPY' in bundle['summary']['plain_language'][0]
    assert verify_bundle(bundle).ok


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


def test_cli_checkpoint_distinguishes_verified_snapshot_from_valid_prefix(tmp_path, bundle, world):
    import subprocess
    import sys
    from pathlib import Path
    from flightrecorder.evidence import summarise

    _, signed, key = world
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_bundle.py'
    checkpoint = tmp_path / 'checkpoint.json'
    data = {'checkpoint_version': 1, 'mandate_id': bundle['mandate_id'],
            'head_hash': bundle['head_hash'], 'public_key': key}
    checkpoint.write_text(json.dumps(data), encoding='utf-8')
    evidence = tmp_path / 'evidence.json'

    def run(snapshot, *options):
        evidence.write_text(json.dumps(snapshot), encoding='utf-8')
        return subprocess.run([sys.executable, str(script), str(evidence), *options],
                              capture_output=True, text=True, timeout=10)

    options = ('--checkpoint', str(checkpoint))
    result = run(bundle, *options)
    assert result.returncode == 0 and 'checkpoint: matched' in result.stdout
    shortened = copy.deepcopy(bundle)
    shortened['events'].pop()
    shortened['head_hash'] = shortened['events'][-1]['hash']
    shortened['summary'] = summarise(signed.mandate.to_payload(), shortened['events'])
    assert run(shortened).returncode == 0
    rejected = run(shortened, *options)
    assert rejected.returncode == 1 and 'independently saved' in rejected.stdout
    assert run([], *options).returncode == 1

    checkpoint.write_text(json.dumps({**data, 'public_key': '00' * 32}), encoding='utf-8')
    wrong_signer = run(bundle, *options)
    assert wrong_signer.returncode == 1 and 'trusted key' in wrong_signer.stdout
    checkpoint.write_text(json.dumps({**data, 'checkpoint_version': True}), encoding='utf-8')
    invalid = run(bundle, *options)
    assert invalid.returncode == 1 and 'invalid checkpoint' in invalid.stderr


@pytest.mark.parametrize('mode', ['no credentials', 'unreachable', 'no orders', 'match', 'invalid bundle'])
def test_cli_requires_requested_paypal_witness(tmp_path, bundle, world, monkeypatch, capsys, mode):
    import runpy
    import sys
    from pathlib import Path
    from flightrecorder.paypal import PayPalError

    orch, signed, _ = world
    if mode == 'no orders':
        empty = Ledger()
        Orchestrator(empty, orch.paypal).grant(signed)
        bundle = build_bundle(empty, signed)
    elif mode == 'invalid bundle':
        bundle['head_hash'] = 'invalid'
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(bundle), encoding='utf-8')
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_bundle.py'
    monkeypatch.setattr(sys, 'argv', [str(script), str(path), '--paypal'])
    lookups = []

    def config():
        if mode == 'no credentials':
            raise PayPalError('missing credentials')
        return PayPalConfig('id', 'secret')

    def get_order(self, order_id):
        lookups.append(order_id)
        if mode == 'unreachable':
            raise PayPalError('not found', 404)
        return _paypal_with_descriptions(orch)(order_id)

    monkeypatch.setattr(PayPalConfig, 'from_env', staticmethod(config))
    monkeypatch.setattr(PayPalClient, 'get_order', get_order)
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(script))
    assert exc.value.code == (0 if mode == 'match' else 1)
    assert bool(lookups) == (mode in ('unreachable', 'match'))
    output = capsys.readouterr().out
    assert ('RESULT: bundle verifies' in output) == (mode == 'match')


def _paypal_with_descriptions(orch):
    """A fake PayPal that stored what the orchestrator really sent."""
    sent = {}
    for call in orch.paypal._transport.calls:
        if call[1] == "/v2/checkout/orders" and call[0] == "POST":
            sent[f"ORD{len(sent) + 1}"] = call[2]["purchase_units"][0]["description"]
    return lambda order_id: {"purchase_units": [{"description": sent[order_id]}]}


def test_paypal_witness_matches_an_honest_log(world):
    from flightrecorder.evidence import check_paypal_witness
    orch, signed, _ = world
    rows = check_paypal_witness(build_bundle(orch.ledger, signed), _paypal_with_descriptions(orch))
    assert len(rows) == 1 and rows[0]["matches_paypal"] is True and rows[0]["logged_head_matches_chain"]


def test_fully_rebuilt_chain_passes_offline_but_paypal_witness_catches_it(world):
    """The strongest attack on a hash chain: edit history, then recompute every hash.
    The bundle verifies offline, but PayPal's stored copy of the head no longer matches."""
    from flightrecorder.evidence import check_paypal_witness
    from flightrecorder.ledger import compute_event_hash
    orch, signed, pub = world
    honest = json.loads(json.dumps(build_bundle(orch.ledger, signed)))
    paypal = _paypal_with_descriptions(orch)

    forged = copy.deepcopy(honest)
    forged["events"][2]["payload"]["verdict"]["reasons"][0]["message"] = "rewritten after the fact"
    prev = forged["events"][0]["hash"]
    for ev in forged["events"][1:]:
        ev["prev_hash"] = prev
        ev["hash"] = compute_event_hash(ev["seq"], ev["ts"], ev["type"], ev["mandate_id"], ev["order_ref"],
                                        ev["payload"], ev["prev_hash"])
        prev = ev["hash"]
    forged["head_hash"] = prev

    assert verify_bundle(forged, trusted_public_key=pub).ok            # self-consistent: offline check alone is fooled
    rows = check_paypal_witness(forged, paypal)
    assert rows[0]["matches_paypal"] is False and "DIFFERS" in rows[0]["detail"]


def test_paypal_witness_reports_unreachable_orders_as_not_checked(world):
    from flightrecorder.evidence import check_paypal_witness
    orch, signed, _ = world
    def boom(_id):
        raise RuntimeError("404")
    rows = check_paypal_witness(build_bundle(orch.ledger, signed), boom)
    assert rows[0]["matches_paypal"] is None and "could not read" in rows[0]["detail"]
