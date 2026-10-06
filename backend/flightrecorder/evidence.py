"""Evidence bundles: what an agent was allowed to do, and what it did.

``build_bundle`` packages the signed mandate and the whole hash-chained log
into one JSON document. ``verify_bundle`` checks it with no database and no
network: it recomputes every hash, checks the mandate signature, confirms
the logged mandate is the signed one, and regenerates the summary from the
events so an edited summary is caught too.

A bundle carries the full chain from the first event, not only one
mandate's events, because the chain can only be checked end to end. That
means a bundle can include other mandates' events; see DECISIONS.md.

Truncating the newest events is only detectable against a head hash the
verifier obtained elsewhere, so ``verify_bundle`` accepts one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import ledger as L
from .canonical import digest
from .ledger import GENESIS_HASH, Ledger, compute_event_hash
from .mandate import SignedMandate, verify_mandate

BUNDLE_VERSION = 1


def _money(minor: int, currency: str) -> str:
    return f"{minor // 100}.{minor % 100:02d} {currency}"


def summarise(mandate_payload: dict, events: list[dict]) -> dict:
    """Derive the allowed-vs-happened summary purely from the events."""
    m = mandate_payload
    cur = m["currency"]
    mine = [e for e in events if e["mandate_id"] == m["mandate_id"]]

    intents: dict[str, dict] = {}
    for e in mine:
        iid = e["payload"].get("intent_id")
        if e["type"] == L.INTENT_SUBMITTED:
            it = e["payload"]["intent"]
            total = sum(i["unit_price_minor"] * i["quantity"] for i in it["items"])
            intents[iid] = {
                "intent_id": iid, "merchant": it["merchant_id"], "amount_minor": total,
                "currency": it["currency"], "verdict": None, "reason_codes": [],
                "approved_by": None, "order_id": None, "captured": False, "refunded": False,
                "voided": False,
            }
        elif iid in intents and e["type"] == L.VERDICT_ISSUED:
            v = e["payload"]["verdict"]
            intents[iid]["verdict"] = v["decision"]
            intents[iid]["reason_codes"] = [r["code"] for r in v["reasons"]]
        elif iid in intents and e["type"] == L.APPROVAL_GRANTED:
            intents[iid]["approved_by"] = e["payload"].get("approver")
        elif iid in intents and e["type"] == L.ORDER_CREATED:
            intents[iid]["order_id"] = e["order_ref"]
    by_order = {i["order_id"]: i for i in intents.values() if i["order_id"]}
    for e in mine:
        order = by_order.get(e["order_ref"])
        if order and e["type"] == L.ORDER_CAPTURED:
            order["captured"] = True
        elif order and e["type"] == L.ORDER_REFUNDED:
            order["refunded"] = True
        elif order and e["type"] == L.ORDER_VOIDED:
            order["voided"] = True

    rows = list(intents.values())
    lines = [
        f"Mandate {m['mandate_id']}: agent '{m['agent_id']}' was authorised by "
        f"'{m['principal_id']}' ({m['purpose']!r}) to spend up to "
        f"{_money(m['total_budget_minor'], cur)} in total, at most "
        f"{_money(m['per_order_cap_minor'], cur)} per order, across at most "
        f"{m['max_orders']} order(s), at {', '.join(m['allowed_merchants'])} only, "
        f"in categories {', '.join(m['allowed_categories'])}, "
        f"valid {m['created_at']} to {m['expires_at']}."
    ]
    for r in rows:
        what = f"{_money(r['amount_minor'], r['currency'])} at {r['merchant']}"
        if r["verdict"] == "DENY":
            lines.append(f"Refused before any PayPal order existed: {what} ({', '.join(r['reason_codes'])}).")
        elif r["order_id"]:
            extra = f", approved by {r['approved_by']}" if r["approved_by"] else ""
            state = "refunded" if r["refunded"] else "captured" if r["captured"] else \
                "voided" if r["voided"] else "created"
            lines.append(f"PayPal order {r['order_id']} {state}: {what}{extra}.")
        else:
            lines.append(f"No order resulted: {what} (verdict {r['verdict']}).")
    return {
        "intents": rows,
        "orders_created": sum(1 for r in rows if r["order_id"]),
        "intents_denied": sum(1 for r in rows if r["verdict"] == "DENY"),
        "plain_language": lines,
    }


def build_bundle(ledger: Ledger, signed: SignedMandate) -> dict:
    events = [e.to_dict() for e in ledger.events()]
    head = events[-1]["hash"] if events else GENESIS_HASH
    mandate_id = signed.mandate.mandate_id
    return {
        "bundle_version": BUNDLE_VERSION,
        "mandate_id": mandate_id,
        "signed_mandate": signed.to_dict(),
        "mandate_digest": signed.mandate.digest(),
        "events": events,
        "head_hash": head,
        "ledger_self_check": {"ok": ledger.verify().ok},
        "summary": summarise(signed.mandate.to_payload(), events),
    }


@dataclass
class BundleCheck:
    ok: bool = True
    problems: list[str] = field(default_factory=list)
    first_bad_seq: int | None = None
    signature_ok: bool = False
    chain_ok: bool = False
    head_hash: str | None = None

    def fail(self, message: str, seq: int | None = None) -> None:
        self.ok = False
        self.problems.append(message)
        if seq is not None and self.first_bad_seq is None:
            self.first_bad_seq = seq


def verify_bundle(
    bundle: dict,
    *,
    trusted_public_key: str | None = None,
    expected_head_hash: str | None = None,
) -> BundleCheck:
    """Check a bundle with no database. Never raises on malformed input."""
    check = BundleCheck()
    try:
        _verify(bundle, check, trusted_public_key, expected_head_hash)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError) as error:
        check.fail(f"bundle is malformed ({type(error).__name__})")
    return check


def _verify(bundle, check, trusted_key, expected_head):
    signed = SignedMandate.from_dict(bundle["signed_mandate"])
    mandate_id = signed.mandate.mandate_id

    check.signature_ok = verify_mandate(signed, trusted_key)
    if not check.signature_ok:
        check.fail("mandate signature does not verify"
                   + (" against the trusted key" if trusted_key else ""))
    if bundle["mandate_digest"] != signed.mandate.digest():
        check.fail("mandate digest does not match the mandate")
    if bundle["mandate_id"] != mandate_id:
        check.fail("bundle mandate id does not match the mandate")

    events = bundle["events"]
    prev, seq_expected = GENESIS_HASH, 1
    chain_ok = True
    for ev in events:
        seq = ev["seq"]
        if seq != seq_expected:
            check.fail("events are missing, duplicated or out of order", seq)
            chain_ok = False
            break
        if ev["prev_hash"] != prev:
            check.fail("previous-hash link is broken", seq)
            chain_ok = False
            break
        recomputed = compute_event_hash(
            seq, ev["ts"], ev["type"], ev["mandate_id"], ev["order_ref"],
            ev["payload"], ev["prev_hash"],
        )
        if recomputed != ev["hash"]:
            check.fail("event contents do not match its hash", seq)
            chain_ok = False
            break
        prev, seq_expected = ev["hash"], seq + 1
    check.chain_ok = chain_ok
    check.head_hash = prev

    if bundle["head_hash"] != prev:
        check.fail("bundle head hash does not match the last event")
    if expected_head is not None and expected_head != prev:
        check.fail("head hash differs from the independently saved one "
                   "(events may have been removed or added)")

    granted = [e for e in events
               if e["type"] == L.MANDATE_GRANTED and e["mandate_id"] == mandate_id]
    if len(granted) != 1:
        check.fail("log does not contain exactly one grant for this mandate")
    elif granted[0]["payload"] != bundle["signed_mandate"]:
        check.fail("mandate in the log differs from the signed mandate in the bundle",
                   granted[0]["seq"])

    if chain_ok and bundle["summary"] != summarise(signed.mandate.to_payload(), events):
        check.fail("summary does not match the events")

    # Canonical form must be hashable; this also rejects smuggled floats.
    digest(bundle["signed_mandate"])
