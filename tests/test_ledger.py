from datetime import timedelta

import pytest

from flightrecorder import ledger as L
from flightrecorder.guard import Decision, MandateState, evaluate
from flightrecorder.ledger import GENESIS_HASH, Ledger

from conftest import T0, make_intent


def filled_ledger() -> Ledger:
    log = Ledger()
    log.append(L.MANDATE_GRANTED, {"purpose": "shoes"}, mandate_id="m-1", ts=T0)
    log.append(L.INTENT_SUBMITTED, {"total_minor": 8_999}, mandate_id="m-1", ts=T0)
    log.append(L.VERDICT_ISSUED, {"decision": "ALLOW"}, mandate_id="m-1", ts=T0)
    log.append(
        L.ORDER_CREATED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1", ts=T0
    )
    log.append(
        L.ORDER_CAPTURED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1", ts=T0
    )
    return log


def test_empty_ledger_verifies_and_has_genesis_head():
    log = Ledger()
    assert log.head_hash() == GENESIS_HASH
    check = log.verify()
    assert check.ok and check.checked == 0


def test_events_are_chained():
    log = filled_ledger()
    events = log.events()
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]
    assert events[0].prev_hash == GENESIS_HASH
    for previous, current in zip(events, events[1:]):
        assert current.prev_hash == previous.hash
    assert log.head_hash() == events[-1].hash
    check = log.verify()
    assert check.ok and check.checked == 5


def test_same_events_give_same_hashes():
    assert filled_ledger().head_hash() == filled_ledger().head_hash()


def test_editing_a_payload_is_detected():
    log = filled_ledger()
    log._db.execute("UPDATE events SET payload = ? WHERE seq = 4", ('{"amount_minor":1}',))
    log._db.commit()
    check = log.verify()
    assert not check.ok
    assert check.first_bad_seq == 4


def test_editing_a_timestamp_or_type_is_detected():
    for column, value in (("ts", "2020-01-01T00:00:00Z"), ("type", L.ORDER_VOIDED)):
        log = filled_ledger()
        log._db.execute(f"UPDATE events SET {column} = ? WHERE seq = 2", (value,))
        log._db.commit()
        check = log.verify()
        assert not check.ok and check.first_bad_seq == 2


def test_recomputing_one_hash_still_breaks_the_next_link():
    log = filled_ledger()
    row = log._db.execute("SELECT * FROM events WHERE seq = 3").fetchone()
    new_payload = {"decision": "DENY"}
    new_hash = L._event_hash(
        3, row["ts"], row["type"], row["mandate_id"], row["order_ref"], new_payload, row["prev_hash"]
    )
    log._db.execute(
        "UPDATE events SET payload = ?, hash = ? WHERE seq = 3", ('{"decision":"DENY"}', new_hash)
    )
    log._db.commit()
    check = log.verify()
    assert not check.ok
    assert check.first_bad_seq == 4  # event 3 is self-consistent; the link from 4 is not


def test_deleting_an_event_is_detected():
    for seq in (1, 3, 5):
        log = filled_ledger()
        log._db.execute("DELETE FROM events WHERE seq = ?", (seq,))
        log._db.commit()
        check = log.verify()
        if seq == 5:
            # Removing the newest event leaves a valid shorter chain. That is
            # the known limit of a hash chain, and why the head hash is also
            # published outside the database.
            assert check.ok and check.checked == 4
        else:
            assert not check.ok


def test_truncation_is_caught_by_a_saved_head_hash():
    log = filled_ledger()
    published_head = log.head_hash()
    log._db.execute("DELETE FROM events WHERE seq = 5")
    log._db.commit()
    assert log.verify().ok
    assert log.head_hash() != published_head


def test_inserting_an_event_in_the_middle_is_detected():
    log = filled_ledger()
    log._db.execute("UPDATE events SET seq = seq + 10 WHERE seq >= 3")
    log._db.execute(
        "INSERT INTO events (seq, ts, type, mandate_id, order_ref, payload, prev_hash, hash)"
        " VALUES (3, '2026-10-06T12:00:00Z', 'APPROVAL_GRANTED', 'm-1', NULL, '{}', 'x', 'y')"
    )
    log._db.commit()
    assert not log.verify().ok


def test_corrupt_payload_is_reported_not_raised():
    log = filled_ledger()
    log._db.execute("UPDATE events SET payload = 'not json' WHERE seq = 2")
    log._db.commit()
    check = log.verify()
    assert not check.ok and check.first_bad_seq == 2


def test_floats_are_refused_in_payloads():
    log = Ledger()
    with pytest.raises(TypeError):
        log.append(L.ORDER_CREATED, {"amount": 89.99}, mandate_id="m-1")
    assert log.events() == []


def test_filters_by_mandate_and_order():
    log = filled_ledger()
    log.append(L.MANDATE_GRANTED, {}, mandate_id="m-2", ts=T0)
    assert len(log.events(mandate_id="m-1")) == 5
    assert len(log.events(mandate_id="m-2")) == 1
    assert [e.type for e in log.events(order_ref="O-1")] == [L.ORDER_CREATED, L.ORDER_CAPTURED]


def test_ledger_persists_to_disk(tmp_path):
    path = str(tmp_path / "recorder.sqlite")
    first = Ledger(path)
    first.append(L.MANDATE_GRANTED, {"purpose": "shoes"}, mandate_id="m-1", ts=T0)
    head = first.head_hash()
    first.close()
    second = Ledger(path)
    assert second.head_hash() == head
    second.append(L.MANDATE_REVOKED, {}, mandate_id="m-1", ts=T0)
    assert second.verify().ok and second.verify().checked == 2


# -- derived mandate state ---------------------------------------------------


def test_state_of_unknown_mandate_is_empty():
    assert Ledger().mandate_state("nope") == MandateState()


def test_created_order_reserves_budget_before_capture():
    log = Ledger()
    log.append(L.ORDER_CREATED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1")
    assert log.mandate_state("m-1") == MandateState(committed_minor=8_999, order_count=1)


def test_capture_does_not_double_count():
    state = filled_ledger().mandate_state("m-1")
    assert state == MandateState(committed_minor=8_999, order_count=1)


def test_capture_amount_overrides_created_amount():
    log = Ledger()
    log.append(L.ORDER_CREATED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1")
    log.append(L.ORDER_CAPTURED, {"amount_minor": 9_499}, mandate_id="m-1", order_ref="O-1")
    assert log.mandate_state("m-1").committed_minor == 9_499


def test_void_releases_budget_and_order_slot():
    log = Ledger()
    log.append(L.ORDER_CREATED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1")
    log.append(L.ORDER_VOIDED, {}, mandate_id="m-1", order_ref="O-1")
    assert log.mandate_state("m-1") == MandateState()


def test_refund_releases_budget_but_not_order_slot():
    log = filled_ledger()
    log.append(L.ORDER_REFUNDED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1")
    assert log.mandate_state("m-1") == MandateState(committed_minor=0, order_count=1)


def test_revocation_shows_in_state():
    log = filled_ledger()
    log.append(L.MANDATE_REVOKED, {"by": "user-1"}, mandate_id="m-1")
    assert log.mandate_state("m-1").revoked


def test_other_mandates_do_not_leak_into_state():
    log = filled_ledger()
    log.append(L.ORDER_CREATED, {"amount_minor": 50_000}, mandate_id="m-2", order_ref="O-9")
    assert log.mandate_state("m-1").committed_minor == 8_999


# -- guard and ledger together -------------------------------------------------


def test_second_order_is_blocked_once_the_first_is_logged(signed):
    log = Ledger()
    now = T0 + timedelta(hours=1)
    intent = make_intent()

    first = evaluate(signed, intent, log.mandate_state("m-1"), now)
    assert first.decision is Decision.ALLOW
    log.append(L.ORDER_CREATED, {"amount_minor": 8_999}, mandate_id="m-1", order_ref="O-1")

    second = evaluate(signed, intent, log.mandate_state("m-1"), now)
    assert second.decision is Decision.DENY


def test_revoking_in_the_log_blocks_the_next_purchase(signed):
    log = Ledger()
    now = T0 + timedelta(hours=1)
    assert evaluate(signed, make_intent(), log.mandate_state("m-1"), now).decision is Decision.ALLOW
    log.append(L.MANDATE_REVOKED, {"by": "user-1"}, mandate_id="m-1")
    assert evaluate(signed, make_intent(), log.mandate_state("m-1"), now).decision is Decision.DENY
