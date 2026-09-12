import json

import pytest

from teller.schema import (AppRef, Capability, Checkpoint, Condition, Detector, Locator, OutputSpec, ParamSpec,
                           Provenance, Step, Target, Value, fill)


def _target(name="Search", role="button"):
    return Target(describe=f"the {name} button", role=role, name=name, frame=["main"],
                  locators=[Locator(strategy="role", value=name), Locator(strategy="css", value="input")])


def _capability() -> Capability:
    return Capability(
        id="demo", title="Demo", description="Demo capability", app=AppRef(profile="meridian", entry="/main/home"),
        inputs={"member_id": ParamSpec(type="integer", pattern=r"^\d{6}$")},
        outputs={"balance": OutputSpec(type="money", source=_target("$1.00", "cell"), parse="money")},
        steps=[Step(id="s1", action="click", target=_target(), on_url="/main/members",
                    expect=Checkpoint(url="/main/members/{member_id}", heading="Member Profile"))],
        success=Checkpoint(url="/main/members/{member_id}"),
        provenance=Provenance(recorded_at="now", model="scripted", discovery_run="d1", teller_version="0"))


def test_round_trip(tmp_path):
    cap = _capability()
    path = tmp_path / "demo.json"
    cap.save(str(path))
    again = Capability.load(str(path))
    assert again == cap
    assert "timeout_ms" in path.read_text()


def test_fingerprint_binds_approval_to_flow():
    cap = _capability()
    fp = cap.fingerprint()
    cap.status = "approved"
    cap.provenance.approved_fingerprint = fp
    assert cap.approval_is_current()
    cap.steps[0].expect.heading = "Something else"
    assert not cap.approval_is_current(), "an edit after approval must invalidate it"
    cap.provenance.approved_by = "someone"  # provenance is not part of the fingerprint
    assert cap.fingerprint() != fp


def test_value_needs_exactly_one_side():
    with pytest.raises(ValueError):
        Value()
    with pytest.raises(ValueError):
        Value(literal="a", param="b")
    assert Value(param="member_id").render({"member_id": "42"}) == "42"
    assert Value(literal="/m/{member_id}").render({"member_id": "42"}) == "/m/42"


def test_step_shape_validation():
    with pytest.raises(ValueError):
        Step(id="x", action="click")  # click needs a target
    with pytest.raises(ValueError):
        Step(id="x", action="type", target=_target())  # type needs a value
    with pytest.raises(ValueError):
        Step(id="x", action="press")


def test_condition_shape_validation():
    with pytest.raises(ValueError):
        Condition(id="c", kind="recoverable", when=Detector(text="x"))
    with pytest.raises(ValueError):
        Condition(id="c", kind="outcome", when=Detector(text="x"))
    with pytest.raises(ValueError):
        Detector()


def test_fill_leaves_unknown_placeholders():
    assert fill("/m/{member_id}/{other}", {"member_id": "1"}) == "/m/1/{other}"
    assert fill(None, {}) is None


def test_tool_schema_is_valid_function_calling_shape():
    schema = _capability().to_tool_schema()
    assert schema["name"] == "demo"
    assert schema["input_schema"]["properties"]["member_id"]["type"] == "integer"
    assert schema["input_schema"]["required"] == ["member_id"]
    assert "balance" in schema["description"]
    json.dumps(schema)
