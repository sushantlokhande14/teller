import pytest

from teller.agent import find_ref
from teller.replay import parse_value, url_matches, validate_inputs
from teller.schema import AppRef, Capability, Checkpoint, ParamSpec, Provenance
from teller.surface.base import Element, Observation


def test_url_matches_with_params_and_wildcards():
    inputs = {"member_id": "100234"}
    assert url_matches("/main/members/{member_id}", "/main/members/100234", inputs)
    assert not url_matches("/main/members/{member_id}", "/main/members/999999", inputs)
    assert url_matches("/main/members/{member_id}/subaccounts/*", "/main/members/100234/subaccounts/100234-S07", inputs)
    assert not url_matches("/main/members/{member_id}/subaccounts/*", "/main/members/100234/subaccounts/", inputs)
    assert not url_matches("/a/*", "/a/b/c", inputs)
    assert url_matches("/x?tab=2", "/x?tab=2", inputs) and not url_matches("/x?tab=2", "/x?tab=3", inputs)


def test_parse_value():
    assert parse_value("$4,812.55", "money") == "4812.55"
    assert parse_value("-12", "money") == "-12.00"
    assert parse_value(" 42 ", "integer") == 42
    assert parse_value("03/14/2011", "date") == "2011-03-14"
    assert parse_value("Priya", "text") == "Priya"
    with pytest.raises(ValueError):
        parse_value("Priya", "money")


def _cap(**inputs) -> Capability:
    return Capability(id="c", title="t", description="d", app=AppRef(profile="meridian"), inputs=inputs, steps=[],
                      success=Checkpoint(), provenance=Provenance(recorded_at="", model="", discovery_run="", teller_version=""))


def test_validate_inputs():
    cap = _cap(member_id=ParamSpec(type="integer", pattern=r"^\d{6}$"), note=ParamSpec(required=False))
    assert validate_inputs(cap, {"member_id": "100234"}) is None
    assert "missing" in validate_inputs(cap, {})
    assert "integer" in validate_inputs(cap, {"member_id": "abc"})
    assert "match" in validate_inputs(cap, {"member_id": "12"})
    assert "unexpected" in validate_inputs(cap, {"member_id": "100234", "extra": "x"})


def test_scripted_model_matching_skips_the_label_cell():
    els = [Element(ref=1, role="cell", name="Name", frame=["main"], bbox=(0, 0, 1, 1), row_label="Name"),
           Element(ref=2, role="cell", name="Priya", frame=["main"], bbox=(0, 0, 1, 1), row_label="Name"),
           Element(ref=3, role="cell", name="$1", frame=["main"], bbox=(0, 0, 1, 1), row_label="100-S01",
                   row_cells=["100-S01", "Savings", "$1"], col_header="Current Balance")]
    obs = Observation(url="", path="/", title="", heading=None, text="", elements=els, http_status=200, frames=[], taken_at="")
    assert find_ref(obs, {"role": "cell", "row": "Name"}) == 2
    assert find_ref(obs, {"role": "cell", "column": "Current Balance", "row_contains": "Savings"}) == 3
    with pytest.raises(KeyError):
        find_ref(obs, {"role": "button", "name": "Nope"})
