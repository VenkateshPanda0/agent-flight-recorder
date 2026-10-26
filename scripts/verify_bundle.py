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

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("bundle")
ap.add_argument("--head")
ap.add_argument("--key")
args = ap.parse_args()
try:
    bundle = json.loads(Path(args.bundle).read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
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
        print(f"anchor at event #{a['seq']}: {state}, independent timestamp {a['gen_time']} ({a['tsa']})")
    print(f"events after the last anchor (not independently pinned): {check.events_after_last_anchor}")
else:
    print("no external timestamp anchors in this bundle: removal of the newest events is undetectable without --head")
if not args.head:
    print("note: no --head given, so removal of the newest events cannot be detected")
if not args.key:
    print("note: no --key given, so the signer is not checked against a trusted key")
if check.ok:
    print("RESULT: bundle verifies")
else:
    for p in check.problems:
        print("PROBLEM:", p + (f" (event #{check.first_bad_seq})" if check.first_bad_seq else ""))
    print("RESULT: bundle FAILED")
sys.exit(0 if check.ok else 1)
