"""The flight recorder: an append-only, tamper-evident event log.

Every event stores the hash of the event before it. Changing, removing or
reordering any past event breaks every hash after it, so ``verify`` can
show that the record of what an agent did has not been edited after the
fact. The log is also the source of truth for how much of a mandate's
budget has been used.

Tamper-evident is not tamper-proof: someone with write access to the
database could rebuild the whole chain. Publishing the head hash somewhere
they cannot edit (for example inside the PayPal order) closes that gap.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime

from .canonical import canonical_json, digest
from .guard import MandateState
from .mandate import to_iso, utc_now

GENESIS_HASH = "0" * 64

# Event types
MANDATE_GRANTED = "MANDATE_GRANTED"
MANDATE_REVOKED = "MANDATE_REVOKED"
INTENT_SUBMITTED = "INTENT_SUBMITTED"
VERDICT_ISSUED = "VERDICT_ISSUED"
APPROVAL_GRANTED = "APPROVAL_GRANTED"
APPROVAL_DENIED = "APPROVAL_DENIED"
ORDER_CREATED = "ORDER_CREATED"
ORDER_CAPTURED = "ORDER_CAPTURED"
ORDER_FAILED = "ORDER_FAILED"
ORDER_VOIDED = "ORDER_VOIDED"
ORDER_REFUNDED = "ORDER_REFUNDED"
DISPUTE_OPENED = "DISPUTE_OPENED"
EVIDENCE_SUBMITTED = "EVIDENCE_SUBMITTED"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,
    type       TEXT NOT NULL,
    mandate_id TEXT,
    order_ref  TEXT,
    payload    TEXT NOT NULL,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_mandate ON events (mandate_id);
CREATE INDEX IF NOT EXISTS idx_events_order ON events (order_ref);
"""


@dataclass(frozen=True)
class Event:
    seq: int
    ts: str
    type: str
    mandate_id: str | None
    order_ref: str | None
    payload: dict
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "mandate_id": self.mandate_id,
            "order_ref": self.order_ref,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class ChainCheck:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    problem: str | None = None


def _event_hash(
    seq: int,
    ts: str,
    type_: str,
    mandate_id: str | None,
    order_ref: str | None,
    payload: dict,
    prev_hash: str,
) -> str:
    return digest(
        {
            "seq": seq,
            "ts": ts,
            "type": type_,
            "mandate_id": mandate_id,
            "order_ref": order_ref,
            "payload": payload,
            "prev_hash": prev_hash,
        }
    )


class Ledger:
    def __init__(self, path: str = ":memory:") -> None:
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        self._db.close()

    # -- writing -----------------------------------------------------------

    def append(
        self,
        type_: str,
        payload: dict,
        *,
        mandate_id: str | None = None,
        order_ref: str | None = None,
        ts: datetime | None = None,
    ) -> Event:
        stamp = to_iso(ts or utc_now())
        # Round-trip through canonical JSON so what is hashed is exactly
        # what is stored, and unhashable content is refused up front.
        stored_payload = canonical_json(payload).decode("utf-8")
        clean_payload = json.loads(stored_payload)

        with self._lock:
            row = self._db.execute(
                "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            seq = (row["seq"] + 1) if row else 1
            prev_hash = row["hash"] if row else GENESIS_HASH
            event_hash = _event_hash(
                seq, stamp, type_, mandate_id, order_ref, clean_payload, prev_hash
            )
            self._db.execute(
                "INSERT INTO events (seq, ts, type, mandate_id, order_ref, payload, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, stamp, type_, mandate_id, order_ref, stored_payload, prev_hash, event_hash),
            )
            self._db.commit()

        return Event(seq, stamp, type_, mandate_id, order_ref, clean_payload, prev_hash, event_hash)

    # -- reading -----------------------------------------------------------

    @staticmethod
    def _to_event(row: sqlite3.Row) -> Event:
        return Event(
            seq=row["seq"],
            ts=row["ts"],
            type=row["type"],
            mandate_id=row["mandate_id"],
            order_ref=row["order_ref"],
            payload=json.loads(row["payload"]),
            prev_hash=row["prev_hash"],
            hash=row["hash"],
        )

    def events(
        self, *, mandate_id: str | None = None, order_ref: str | None = None
    ) -> list[Event]:
        query = "SELECT * FROM events"
        clauses, params = [], []
        if mandate_id is not None:
            clauses.append("mandate_id = ?")
            params.append(mandate_id)
        if order_ref is not None:
            clauses.append("order_ref = ?")
            params.append(order_ref)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY seq"
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [self._to_event(row) for row in rows]

    def head_hash(self) -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT hash FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        return row["hash"] if row else GENESIS_HASH

    # -- integrity ---------------------------------------------------------

    def verify(self) -> ChainCheck:
        """Recompute every hash from the start and report the first break."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM events ORDER BY seq").fetchall()

        expected_prev = GENESIS_HASH
        expected_seq = 1
        for count, row in enumerate(rows):
            seq = row["seq"]
            if seq != expected_seq:
                return ChainCheck(False, count, expected_seq, "missing or out-of-order event")
            if row["prev_hash"] != expected_prev:
                return ChainCheck(False, count, seq, "previous-hash link is broken")
            try:
                payload = json.loads(row["payload"])
                recomputed = _event_hash(
                    seq,
                    row["ts"],
                    row["type"],
                    row["mandate_id"],
                    row["order_ref"],
                    payload,
                    row["prev_hash"],
                )
            except (ValueError, TypeError):
                return ChainCheck(False, count, seq, "event payload is unreadable")
            if recomputed != row["hash"]:
                return ChainCheck(False, count, seq, "event contents do not match its hash")
            expected_prev = row["hash"]
            expected_seq += 1
        return ChainCheck(True, len(rows))

    # -- derived state -----------------------------------------------------

    def mandate_state(self, mandate_id: str) -> MandateState:
        """How much of a mandate is used up, according to the log.

        An order counts against the budget from the moment it is created,
        not only once captured, so two orders in flight cannot both fit
        into the last of the budget. Voids and refunds give the money back;
        a voided order also gives back its slot in the order count.
        """
        amounts: dict[str, int] = {}
        voided: set[str] = set()
        refunded = 0
        revoked = False

        for event in self.events(mandate_id=mandate_id):
            ref = event.order_ref or f"seq-{event.seq}"
            if event.type == MANDATE_REVOKED:
                revoked = True
            elif event.type == ORDER_CREATED:
                amounts[ref] = int(event.payload.get("amount_minor", 0))
            elif event.type == ORDER_CAPTURED:
                # Capture may differ from the created amount; trust capture.
                amounts[ref] = int(event.payload.get("amount_minor", amounts.get(ref, 0)))
            elif event.type == ORDER_VOIDED:
                voided.add(ref)
            elif event.type == ORDER_REFUNDED:
                refunded += int(event.payload.get("amount_minor", 0))

        live = {ref: amount for ref, amount in amounts.items() if ref not in voided}
        committed = max(sum(live.values()) - refunded, 0)
        return MandateState(
            committed_minor=committed, order_count=len(live), revoked=revoked
        )
