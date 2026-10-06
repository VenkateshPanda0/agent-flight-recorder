import pytest

from flightrecorder.agent import CATALOGUE, ScriptedProposer, parse_proposal, to_intent


def test_parse_proposal_handles_wrapped_json():
    p = parse_proposal('Sure!\n```json\n{"sku":"TR-1","quantity":1,"rationale":"cheap"}\n```')
    assert p["sku"] == "TR-1"


def test_parse_rejects_non_json():
    with pytest.raises(ValueError):
        parse_proposal("no json here")


def test_intent_uses_catalogue_price_not_model_price():
    intent = to_intent({"sku": "TR-1", "quantity": 2, "rationale": "x"}, CATALOGUE,
                       mandate_id="m", agent_id="a")
    assert intent.computed_total_minor == 17_998 == intent.claimed_total_minor


def test_unknown_sku_raises():
    with pytest.raises(ValueError):
        to_intent({"sku": "ZZ", "quantity": 1}, CATALOGUE, mandate_id="m", agent_id="a")


def test_scripted_injected_listing_is_refused_by_guard(signed, keypair):
    from datetime import datetime
    from conftest import T0
    from flightrecorder.guard import MandateState, evaluate
    p = ScriptedProposer("GC-50", 5).propose("shoes", CATALOGUE)
    intent = to_intent(p, CATALOGUE, mandate_id="m-1", agent_id="agent-shopper")
    v = evaluate(signed, intent, MandateState(), T0, keypair[1])
    assert v.decision.value == "DENY"
