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
from .canonical import canonical_json, digest
from .ledger import GENESIS_HASH, Ledger, compute_event_hash
from .mandate import SignedMandate, verify_mandate
from .paypal import PayPalError, format_amount

BUNDLE_VERSION = 2
MAX_ANCHORS_VERIFIED = 3
BUNDLE_FIELDS = {"bundle_version", "mandate_id", "signed_mandate", "mandate_digest",
                 "events", "head_hash", "summary"}
EVENT_FIELDS = {"seq", "ts", "type", "mandate_id", "order_ref", "payload", "prev_hash", "hash"}


def _money(minor: int, currency: str, version: int) -> str:
    if version == 1:
        return f"{minor // 100}.{minor % 100:02d} {currency}"
    if isinstance(currency, str):
        try:
            sign = "-" if minor < 0 else ""
            return f"{sign}{format_amount(abs(minor), currency)} {currency}"
        except PayPalError:
            pass
    return f"{minor} minor units ({currency})"


def summarise(mandate_payload: dict, events: list[dict], *, version: int = BUNDLE_VERSION) -> dict:
    """Derive the allowed-vs-happened summary purely from the events."""
    m = mandate_payload
    cur = m["currency"]
    mine = [e for e in events if e["mandate_id"] == m["mandate_id"]]

    intents: dict[str, dict] = {}
    unconfirmed: set[str] = set()
    for e in mine:
        iid = e["payload"].get("intent_id")
        if e["type"] == L.INTENT_SUBMITTED:
            if "unhashable_intent" in e["payload"]:
                # Rejected input is kept as text in the chain, not a purchase.
                continue
            it = e["payload"]["intent"]
            items = it["items"]
            total = None
            if all(isinstance(i["unit_price_minor"], int) and isinstance(i["quantity"], int) for i in items):
                total = sum(i["unit_price_minor"] * i["quantity"] for i in items)
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
        elif iid in intents and e["type"] == L.ORDER_REQUESTED:
            unconfirmed.add(iid)
        elif e["type"] == L.ORDER_FAILED:
            unconfirmed.discard(iid)
        elif iid in intents and e["type"] == L.ORDER_CREATED:
            unconfirmed.discard(iid)
            intents[iid]["order_id"] = e["order_ref"]
    by_order = {i["order_id"]: i for i in intents.values() if i["order_id"]}
    capture_states: dict[str, str] = {}
    for e in mine:
        ref = e["order_ref"]
        order = by_order.get(ref)
        if order and version >= 2 and e["type"] == L.CAPTURE_REQUESTED:
            capture_states[ref] = "capture unconfirmed"
        elif order and version >= 2 and e["type"] == L.CAPTURE_REJECTED:
            capture_states[ref] = "capture rejected"
        elif order and version >= 2 and e["type"] == L.CAPTURE_BLOCKED:
            capture_states[ref] = f"capture blocked ({e['payload']['reason_code']})"
        elif order and e["type"] == L.ORDER_CAPTURED:
            status = e["payload"].get("status")
            order["captured"] = order["captured"] or version == 1 or status == "COMPLETED"
            capture_states[ref] = "capture pending" if status == "PENDING" else "capture unconfirmed"
        elif order and e["type"] == L.ORDER_REFUNDED:
            order["refunded"] = True
        elif order and e["type"] == L.ORDER_VOIDED:
            order["voided"] = True

    rows = list(intents.values())
    lines = [
        f"Mandate {m['mandate_id']}: agent '{m['agent_id']}' was authorised by "
        f"'{m['principal_id']}' ({m['purpose']!r}) to spend up to "
        f"{_money(m['total_budget_minor'], cur, version)} in total, at most "
        f"{_money(m['per_order_cap_minor'], cur, version)} per order, across at most "
        f"{m['max_orders']} order(s), at {', '.join(m['allowed_merchants'])} only, "
        f"in categories {', '.join(m['allowed_categories'])}, "
        f"valid {m['created_at']} to {m['expires_at']}."
    ]
    for r in rows:
        amount = "invalid amount" if r["amount_minor"] is None else _money(r["amount_minor"], r["currency"], version)
        what = f"{amount} at {r['merchant']}"
        if r["verdict"] == "DENY":
            lines.append(f"Refused before any PayPal order existed: {what} ({', '.join(r['reason_codes'])}).")
        elif r["order_id"]:
            extra = f", approved by {r['approved_by']}" if r["approved_by"] else ""
            state = "refunded" if r["refunded"] else "captured" if r["captured"] else \
                "voided" if r["voided"] else capture_states.get(r["order_id"], "created")
            lines.append(f"PayPal order {r['order_id']} {state}: {what}{extra}.")
        elif r["intent_id"] in unconfirmed:
            lines.append(f"PayPal order outcome unknown: {what}. Budget and order slot remain reserved.")
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
    anchors: list[dict] = field(default_factory=list)
    events_after_last_anchor: int | None = None
    events_without_verified_timestamp: int | None = None

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
    verify_anchors: bool = False,
) -> BundleCheck:
    """Check a bundle with no database. Never raises on malformed input.

    With ``verify_anchors`` any timestamp-authority tokens in the log are
    checked too (needs the ``openssl`` command); one that fails fails the bundle.
    """
    check = BundleCheck()
    try:
        _verify(bundle, check, trusted_public_key, expected_head_hash, verify_anchors)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError, RecursionError) as error:
        check.fail(f"bundle is malformed ({type(error).__name__})")
    return check


def _verify(bundle, check, trusted_key, expected_head, verify_anchors=False):
    if not isinstance(bundle, dict) or bundle.keys() != BUNDLE_FIELDS:
        check.fail("bundle fields do not match the evidence format")
        return
    version = bundle["bundle_version"]
    if type(version) is not int or version not in (1, BUNDLE_VERSION):
        check.fail("unsupported bundle version")
        return
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
    if not isinstance(events, list):
        check.fail("events must be a list")
        return
    prev, seq_expected = GENESIS_HASH, 1
    chain_ok = True
    for ev in events:
        if not isinstance(ev, dict) or ev.keys() != EVENT_FIELDS:
            check.fail("event fields do not match the evidence format", seq_expected)
            return
        seq = ev["seq"]
        if type(seq) is not int or seq != seq_expected:
            check.fail("events are missing, duplicated or out of order", seq_expected)
            chain_ok = False
            break
        if (not isinstance(ev["payload"], dict) or not isinstance(ev["ts"], str)
                or not isinstance(ev["type"], str)
                or any(ev[key] is not None and not isinstance(ev[key], str) for key in ("mandate_id", "order_ref"))):
            check.fail("event payload or metadata has the wrong type", seq)
            return
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
    elif (canonical_json(granted[0]["payload"]) != canonical_json(bundle["signed_mandate"])
          or canonical_json(signed.to_dict()) != canonical_json(bundle["signed_mandate"])):
        check.fail("mandate in the log differs from the signed mandate in the bundle",
                   granted[0]["seq"])

    if chain_ok:
        summary = summarise(signed.mandate.to_payload(), events, version=version)
        if canonical_json(bundle["summary"]) != canonical_json(summary):
            check.fail("summary does not match the events")

    if chain_ok:
        by_seq = {ev["seq"]: ev for ev in events}
        for ev in events:
            if ev["type"] == L.ORDER_CREATED and "recovered_request_seq" in ev["payload"]:
                head = _order_witness_head(ev, by_seq)
                if head is None or head != ev["payload"].get("log_head_at_creation"):
                    check.fail("recovered order does not match its original request", ev["seq"])
        _check_anchors(events, check, verify_anchors)

def _check_anchors(events: list[dict], check: BundleCheck, verify_anchors: bool) -> None:
    import base64

    from .anchor import ANCHOR_RECORDED, verify_token

    last_seq = events[-1]["seq"] if events else 0
    anchor_events = [ev for ev in events if ev["type"] == ANCHOR_RECORDED]
    # Each token check launches openssl, so bound the work an uploaded bundle can cause.
    # The newest anchors matter most: each one pins every event before it.
    checked_seqs = {ev["seq"] for ev in anchor_events[-MAX_ANCHORS_VERIFIED:]}
    for ev in anchor_events:
        p = ev["payload"]
        entry = {"seq": ev["seq"], "gen_time": p.get("gen_time"), "tsa": p.get("tsa"),
                 "verified": None}
        if p["anchored_hash"] != ev["prev_hash"]:
            check.fail("anchor record does not match its position in the chain", ev["seq"])
        elif verify_anchors and ev["seq"] in checked_seqs:
            res = verify_token(p["anchored_hash"], base64.b64decode(p["token_b64"]))
            entry["verified"] = res.ok
            entry["gen_time"] = res.gen_time if res.ok else None
            if not res.ok:
                check.fail(f"timestamp anchor at event #{ev['seq']}: {res.detail}", ev["seq"])
        check.anchors.append(entry)
    check.events_after_last_anchor = (
        last_seq - check.anchors[-1]["seq"] if check.anchors else last_seq)
    # The timestamp covers the preceding hash, so its own record is outside the proof.
    pinned_through = max((a["seq"] - 1 for a in check.anchors if a["verified"] is True), default=0)
    check.events_without_verified_timestamp = last_seq - pinned_through


WITNESS_PREFIX = "AFR log head "


def _order_witness_head(event: dict, by_seq: dict[int, dict]) -> str | None:
    payload = event["payload"]
    if "recovered_request_seq" not in payload:
        return event["prev_hash"]
    seq = payload["recovered_request_seq"]
    if type(seq) is not int or seq >= event["seq"]:
        return None
    request = by_seq.get(seq)
    if (request is None or request["type"] != L.ORDER_REQUESTED
            or request["mandate_id"] != event["mandate_id"]
            or request["payload"].get("intent_id") != payload.get("intent_id")
            or request["payload"].get("amount_minor") != payload.get("amount_minor")):
        return None
    return request["hash"]


def check_paypal_witness(bundle: dict, get_order) -> list[dict]:
    """Cross-check a bundle against the copy of the log head that PayPal holds.

    Every order is created with the log's head hash in its description, and that
    hash is the preceding hash for an immediate confirmation or the earlier
    request hash for a recovered order. PayPal is
    therefore an independent witness: someone who rewrites history and rebuilds the
    whole chain consistently still cannot change the value PayPal stored.

    ``get_order(order_id)`` returns PayPal's order JSON (e.g. ``PayPalClient.get_order``).
    Returns one row per order: whether the chain agrees with itself, and whether
    PayPal's stored value agrees with the chain (``None`` if PayPal could not be asked).
    """
    rows = []
    by_seq = {event["seq"]: event for event in bundle["events"]}
    for ev in bundle["events"]:
        if ev["type"] != "ORDER_CREATED":
            continue
        order_id = ev["order_ref"]
        chain_head = _order_witness_head(ev, by_seq)
        row = {"seq": ev["seq"], "order_id": order_id, "chain_head": chain_head,
               "logged_head_matches_chain": chain_head is not None and ev["payload"].get("log_head_at_creation") == chain_head,
               "paypal_head": None, "matches_paypal": None, "detail": ""}
        try:
            description = (get_order(order_id)["purchase_units"][0].get("description") or "")
            if description.startswith(WITNESS_PREFIX):
                row["paypal_head"] = description[len(WITNESS_PREFIX):].strip()
                row["matches_paypal"] = row["paypal_head"] == chain_head
                row["detail"] = ("PayPal's stored log head matches the chain" if row["matches_paypal"]
                                 else "PayPal's stored log head DIFFERS from the chain: history before this order was changed")
            else:
                row["detail"] = "PayPal order has no log-head description"
        except Exception as exc:  # network, 404 (sandbox purges old unapproved orders), bad JSON
            row["detail"] = f"could not read the order from PayPal ({type(exc).__name__})"
        rows.append(row)
    return rows
