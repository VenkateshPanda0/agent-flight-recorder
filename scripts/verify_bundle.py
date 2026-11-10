"""Verify an evidence bundle offline: no database, no network, no PayPal.

  python scripts/verify_bundle.py evidence.json [--head HASH] [--key PUBLIC_KEY_HEX]

--head: a head hash you saved or received independently (detects removed events).
--key:  the person's public key you trust (detects a mandate re-signed by someone else).
Exit code 0 if the bundle verifies, 1 if not.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from flightrecorder.evidence import verify_bundle
from flightrecorder.canonical import DuplicateJSONField, unique_json_object

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("bundle")
ap.add_argument("--head")
ap.add_argument("--key")
ap.add_argument("--paypal", action="store_true",
                help="also ask PayPal (sandbox credentials from .env) for its stored copy of the log head at each order")
args = ap.parse_args()
try:
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
except DuplicateJSONField:
    raise SystemExit("cannot read bundle: duplicate JSON field") from None
except (OSError, ValueError, RecursionError) as exc:
    raise SystemExit(f"cannot read bundle: {type(exc).__name__}")
check = verify_bundle(bundle, trusted_public_key=args.key, expected_head_hash=args.head,
                      verify_anchors=True)
print("signature:", "valid" if check.signature_ok else "INVALID")
print("hash chain:", "intact" if check.chain_ok else "BROKEN")
print("head hash:", check.head_hash)
if not check.chain_ok:
    print("timestamp anchors: not checked, because the hash chain is broken")
elif check.anchors:
    for a in check.anchors:
        state = {True: "verified", False: "FAILED", None: "not checked"}[a["verified"]]
        timestamp = f", verified time {a['gen_time']}" if a['verified'] is True and a['gen_time'] else ""
        print(f"anchor at event #{a['seq']}: {state}{timestamp}")
    print(f"events outside verified timestamp coverage: {check.events_without_verified_timestamp}")
else:
    print("no external timestamp anchors in this bundle: removal of the newest events is undetectable without --head")
if not args.head:
    print("note: no --head given, so removal of the newest events cannot be detected")
if not args.key:
    print("note: no --key given, so the signer is not checked against a trusted key")
paypal_ok = True
if args.paypal and not check.ok:
    print("PayPal witness: skipped because offline verification failed")
elif args.paypal:
    from flightrecorder.evidence import check_paypal_witness
    from flightrecorder.paypal import PayPalClient, PayPalConfig, PayPalError
    rows = []
    if not any(event["type"] == "ORDER_CREATED" for event in bundle["events"]):
        print("PayPal witness: no orders in this bundle")
    else:
        try:
            client = PayPalClient(PayPalConfig.from_env())
            rows = check_paypal_witness(bundle, client.get_order)
        except (PayPalError, KeyError, TypeError) as exc:
            print("PayPal cross-check: could not run:", exc)
    for r in rows:
        mark = {True: "matches", False: "DIFFERS", None: "not checked"}[r["matches_paypal"]]
        print(f"PayPal witness, order {r['order_id']} (event #{r['seq']}): {mark}. {r['detail']}")
    paypal_ok = bool(rows) and all(r["matches_paypal"] is True and r["logged_head_matches_chain"] for r in rows)
if check.ok and paypal_ok:
    print("RESULT: bundle verifies")
elif check.ok:
    print("RESULT: offline checks passed, but the requested PayPal witness did not verify")
else:
    for p in check.problems:
        print("PROBLEM:", p + (f" (event #{check.first_bad_seq})" if check.first_bad_seq else ""))
    print("RESULT: bundle FAILED")
sys.exit(0 if check.ok and paypal_ok else 1)
