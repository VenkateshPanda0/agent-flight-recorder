"""Verify an evidence bundle offline: no database, no network, no PayPal.

  python scripts/verify_bundle.py evidence.json --checkpoint checkpoint.json

--head: a head hash you saved or received independently (detects removed events).
--key:  the person's public key you trust (detects a mandate re-signed by someone else).
--checkpoint: a separately retained file with both values and the mandate ID.
Exit code 0 if the bundle verifies, 1 if not.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from flightrecorder.evidence import verify_bundle
from flightrecorder.canonical import DuplicateJSONField, unique_json_object

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("bundle")
ap.add_argument("--head")
ap.add_argument("--key")
ap.add_argument("--checkpoint", type=Path, help="checkpoint saved independently of the evidence being checked")
ap.add_argument("--paypal", action="store_true",
                help="also ask PayPal (sandbox credentials from .env) for its stored copy of the log head at each order")
args = ap.parse_args()
if args.checkpoint and (args.head is not None or args.key is not None):
    ap.error("use --checkpoint on its own or supply --head and --key directly")


def read_checkpoint(path: Path) -> dict:
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
    except (OSError, ValueError, RecursionError) as exc:
        raise SystemExit(f"cannot read checkpoint: {type(exc).__name__}") from None
    fields = {"checkpoint_version", "mandate_id", "head_hash", "public_key"}
    if (not isinstance(checkpoint, dict) or checkpoint.keys() != fields
            or type(checkpoint["checkpoint_version"]) is not int or checkpoint["checkpoint_version"] != 1
            or not isinstance(checkpoint["mandate_id"], str) or not checkpoint["mandate_id"].strip()
            or not all(isinstance(checkpoint[field], str) and re.fullmatch(r"[0-9a-f]{64}", checkpoint[field])
                       for field in ("head_hash", "public_key"))):
        raise SystemExit("invalid checkpoint: expected a version 1 checkpoint with a mandate ID and two hex values")
    return checkpoint


checkpoint = read_checkpoint(args.checkpoint) if args.checkpoint else None
try:
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"), object_pairs_hook=unique_json_object)
except DuplicateJSONField:
    raise SystemExit("cannot read bundle: duplicate JSON field") from None
except (OSError, ValueError, RecursionError) as exc:
    raise SystemExit(f"cannot read bundle: {type(exc).__name__}")
head = checkpoint["head_hash"] if checkpoint else args.head
key = checkpoint["public_key"] if checkpoint else args.key
check = verify_bundle(bundle, trusted_public_key=key, expected_head_hash=head,
                      verify_anchors=True)
if checkpoint and isinstance(bundle, dict) and bundle.get("mandate_id") != checkpoint["mandate_id"]:
    check.fail("mandate ID differs from the saved checkpoint")
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
if checkpoint:
    matches = (check.signature_ok and check.chain_ok and check.head_hash == head
               and isinstance(bundle, dict) and bundle.get("mandate_id") == checkpoint["mandate_id"])
    print("checkpoint:", "matched" if matches else "did not match")
if not head:
    print("note: no saved head supplied, so removal of the newest events cannot be detected")
if not key:
    print("note: no trusted key supplied, so the signer is not checked against a trusted key")
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
