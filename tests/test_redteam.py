from flightrecorder.redteam import SCENARIOS, run_all


def test_every_attack_is_blocked_and_chain_stays_valid():
    r = run_all()
    assert r["misses"] == [], r["misses"]
    assert r["blocked"] == r["attacks"] >= 25
    assert all(x["chain_ok"] for x in r["results"])


def test_in_scope_cases_are_reported_not_counted():
    r = run_all()
    assert len(r["in_scope_not_blocked"]) == sum(s.in_scope for s in SCENARIOS) >= 1
    assert r["attacks"] == len(SCENARIOS) - len(r["in_scope_not_blocked"])


def test_float_price_never_raises():
    r = run_all()
    row = next(x for x in r["results"] if x["name"] == "Float unit price")
    assert row["exception"] is None and row["orders_created"] == 0
