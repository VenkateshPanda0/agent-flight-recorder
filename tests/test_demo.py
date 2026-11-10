import pytest
from flightrecorder.demo import DemoApp


@pytest.fixture
def app():
    return DemoApp(use_real_paypal=False)


def test_full_demo_story(app):
    assert app.run_scenario("buy")["status"] == "ORDER_CREATED"
    over = app.run_scenario("overcap")
    assert over["decision"] == "DENY" and over["order_id"] is None
    inj = app.run_scenario("injection")
    assert inj["decision"] == "DENY"
    pend = app.run_scenario("approval")
    assert pend["status"] == "PENDING_APPROVAL"
    assert app.approve(pend["intent_id"])["status"] == "ORDER_CREATED"
    assert app.state()["summary"]["orders_created"] == 2
    assert app.verify_current()["ok"]
    assert app.verify_bundle(app.bundle())["ok"]


def test_tamper_is_caught_at_the_exact_event(app):
    app.run_scenario("buy")
    edited = app.tamper()["edited_seq"]
    v = app.verify_current()
    assert not v["ok"] and v["first_bad_seq"] == edited


def test_tamper_disabled(app):
    app.allow_tamper = False
    with pytest.raises(PermissionError):
        app.tamper()


@pytest.mark.parametrize('boundary', ['reset', 'another visitor'])
def test_approval_id_cannot_cross_mandate_boundaries(app, boundary):
    from flightrecorder.orchestrator import OrchestratorError

    old = app.run_scenario('approval')
    if boundary == 'reset':
        app.reset()
        current = app
    else:
        current = DemoApp(use_real_paypal=False)
    pending = current.run_scenario('approval')
    assert pending['status'] == 'PENDING_APPROVAL'
    with pytest.raises(OrchestratorError, match='Unknown intent'):
        current.approve(old['intent_id'])
    assert current.state()['summary']['orders_created'] == 0
    assert current.approve(pending['intent_id'])['status'] == 'ORDER_CREATED'


def test_http_roundtrip():
    import json, threading, urllib.request
    from http.server import ThreadingHTTPServer
    from flightrecorder.server import Sessions, make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Sessions(lambda: DemoApp(use_real_paypal=False)), None, True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    def post(p, d, op=opener):
        r = urllib.request.Request(base + p, json.dumps(d).encode(), {"Content-Type": "application/json"})
        return json.load(op.open(r))
    assert post("/api/demo/run", {"scenario": "buy"})["status"] == "ORDER_CREATED"
    assert json.load(opener.open(base + "/api/verify"))["ok"]
    bundle = json.load(opener.open(base + "/api/bundle"))
    assert post("/api/verify-bundle", {"bundle": bundle})["ok"]
    # A second visitor has their own, untouched demo.
    other = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
    assert json.load(other.open(base + "/api/state"))["summary"]["orders_created"] == 0
    post("/api/demo/tamper", {})
    assert not json.load(opener.open(base + "/api/verify"))["ok"]
    assert json.load(other.open(base + "/api/verify"))["ok"]
    srv.shutdown()


def test_dashboard_reset_waits_for_in_flight_purchase(app, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, TimeoutError
    from threading import Event
    from flightrecorder.server import Sessions, make_handler

    sessions = Sessions(lambda: app)
    sid, _ = sessions.get(None)
    handler_type = make_handler(sessions, None, True)
    entered, release, reset_started = Event(), Event(), Event()
    original = app._client.create_order
    old_ledger = app.ledger

    def delayed_create(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    monkeypatch.setattr(app._client, 'create_order', delayed_create)

    def post(path, body):
        handler = object.__new__(handler_type)
        handler.path = path
        handler.headers = {'Cookie': f'afr_sid={sid}'}
        handler._read = lambda: body
        replies = []
        handler._json = lambda status, result: replies.append((status, result))
        if path.endswith('/reset'):
            reset_started.set()
        handler.do_POST()
        return replies[0]

    with ThreadPoolExecutor(2) as pool:
        purchase = pool.submit(post, '/api/demo/run', {'scenario': 'buy'})
        assert entered.wait(5)
        reset = pool.submit(post, '/api/demo/reset', {})
        assert reset_started.wait(5)
        try:
            with pytest.raises(TimeoutError):
                reset.result(timeout=0.1)
            assert app.ledger is old_ledger
        finally:
            release.set()
        assert purchase.result()[1]['status'] == 'ORDER_CREATED'
        assert reset.result()[0] == 200
    assert app.ledger is not old_ledger


def test_webhook_requests_do_not_allocate_or_evict_visitor_sessions(app):
    from flightrecorder.server import Sessions, make_handler

    sessions = Sessions(lambda: app, cap=1)
    sid, _ = sessions.get(None)
    handler = object.__new__(make_handler(sessions, None, True))
    handler.path = '/webhooks/paypal'
    handler.headers = {}
    handler._read = lambda: {}
    replies = []
    handler._json = lambda status, result: replies.append(status)
    handler.do_POST()
    assert replies == [503]
    assert not hasattr(handler, '_sid')
    assert sessions.get(sid)[0] == sid


def test_rate_limiter_blocks_per_visitor_and_overall():
    from flightrecorder.server import RateLimiter
    rl = RateLimiter(per_visitor=2, overall=3, window=10)
    assert rl.allow("a", 0) and rl.allow("a", 1) and not rl.allow("a", 2)
    assert rl.allow("b", 3) and not rl.allow("c", 4)       # overall cap of 3 reached
    assert rl.allow("a", 20)                                 # window passed


def test_dispute_submit_disabled_by_default():
    import json, threading, urllib.error, urllib.request
    from http.server import ThreadingHTTPServer
    from flightrecorder.server import Sessions, make_handler
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Sessions(lambda: DemoApp(use_real_paypal=False)), None, True))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/demo/dispute-evidence",
                                 json.dumps({"dispute_id": "PP-D-1"}).encode(), {"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403
    srv.shutdown()


def test_live_model_scenario_goes_through_the_guard(monkeypatch):
    import flightrecorder.demo as D
    from flightrecorder.agent import ScriptedProposer
    app = DemoApp(use_real_paypal=False)
    monkeypatch.setattr(D, "live_available", lambda: False)
    assert "live" not in app.state()["scenarios"]
    with pytest.raises(RuntimeError):
        app.run_scenario("live")

    monkeypatch.setattr(D, "live_available", lambda: True)
    monkeypatch.setattr(D, "get_live_proposer", lambda: ScriptedProposer("GC-50", 5, "model followed the injection"))
    assert "live" in app.state()["scenarios"]
    r = app.run_scenario("live")
    assert r["decision"] == "DENY" and r["order_id"] is None
    assert app.verify_current()["ok"]


def test_recorded_real_model_runs_replay_through_the_live_guard():
    import json
    from flightrecorder.demo import recorded_runs
    data = recorded_runs()
    assert data and len(data["runs"]) == 24
    assert {r["recorded_decision"] for r in data["runs"]} == {"DENY", "ALLOW", "NEEDS_APPROVAL"}
    app = DemoApp(use_real_paypal=False)
    assert "replay" in app.state()["scenarios"]
    first = app.run_scenario("replay")
    assert first["agent"] == "recorded-llm" and first["recorded"]["number"] == 1
    assert first["decision"] == data["runs"][0]["recorded_decision"]      # the guard agrees with the recording
    if first["decision"] == "DENY":
        assert first["order_id"] is None
    second = app.run_scenario("replay")
    assert second["recorded"]["number"] == 2
    assert app.verify_current()["ok"]


def test_every_recorded_strayed_proposal_is_denied_by_the_guard_in_isolation():
    from flightrecorder.agent import CATALOGUE, to_intent
    from flightrecorder.demo import AGENT_ID, recorded_runs
    strayed = [r for r in recorded_runs()["runs"] if r["recorded_decision"] == "DENY"]
    assert len(strayed) == 15
    for rec in strayed:
        app = DemoApp(use_real_paypal=False)          # fresh mandate state per run, as in the evaluation
        out = app.orch.submit(to_intent(rec["proposal"], CATALOGUE,
                                        mandate_id=app.signed.mandate.mandate_id, agent_id=AGENT_ID))
        assert out.verdict.decision.value == "DENY" and out.order_id is None


def test_paypal_witness_needs_the_real_sandbox_and_checks_orders(monkeypatch):
    import json
    app = DemoApp(use_real_paypal=False)
    with pytest.raises(ValueError):
        app.paypal_witness()                                   # simulated PayPal: nothing to cross-check
    app.paypal_mode = "sandbox"                                # pretend, with a fake PayPal that stored what we sent
    sent = {}
    orig = app._client._transport

    def spy(method, url, headers, body):
        status, data = orig(method, url, headers, body)
        if url.endswith("/v2/checkout/orders") and method == "POST":
            sent[data["id"]] = json.loads(body)["purchase_units"][0]["description"]
        return status, data

    app._client._transport = spy
    app.run_scenario("buy")
    app._client.get_order = lambda oid: {"purchase_units": [{"description": sent[oid]}]}
    rows = app.paypal_witness()
    assert len(rows) == 1 and rows[0]["matches_paypal"] is True

def test_http_reader_rejects_invalid_lengths_and_ambiguous_json():
    from io import BytesIO
    from flightrecorder.server import make_handler
    handler_type = make_handler(None, None, False)
    duplicate = b'{"bundle":{"bundle_version":2,"bundle_version":1}}'
    for length, body in [('-1', b'{}'), ('abc', b'{}'), ('2', b'[]'), (str(len(duplicate)), duplicate)]:
        handler = object.__new__(handler_type)
        handler.headers = {'Content-Length': length}
        handler.rfile = BytesIO(body)
        responses = []
        handler._json = lambda status, data: responses.append(status)
        assert handler._read() is None
        assert responses == [400]


def test_dashboard_witness_skips_paypal_when_local_log_is_tampered(monkeypatch):
    app = DemoApp(use_real_paypal=False, allow_tamper=True)
    app.run_scenario('buy')
    app.tamper()
    app.paypal_mode = 'sandbox'
    lookups = []
    monkeypatch.setattr(app._client, 'get_order', lookups.append)
    with pytest.raises(ValueError, match='local bundle failed verification'):
        app.paypal_witness()
    assert not lookups


def test_rate_limiter_rejected_visitors_do_not_grow_memory():
    from flightrecorder.server import RateLimiter
    limiter = RateLimiter(overall=1)
    assert limiter.allow('first', 0)
    for i in range(100):
        assert not limiter.allow(str(i), 0)
    assert len(limiter._hits) == 1
