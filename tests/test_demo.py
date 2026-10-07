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
