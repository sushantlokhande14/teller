"""
End to end, against the sample app and a real Chromium, with the scripted model in
place of the LLM so the suite runs without an API key. The discovery run the brief
asks for is produced separately (see README).
"""
import json
from pathlib import Path

import pytest

from meridian import app as meridian_app
from teller.cli import main

pytestmark = pytest.mark.e2e

CONTRACT = "contracts/member_savings_balance.json"
SCRIPT = "scripts/scripted_model_lookup.json"


def run_cli(*args: str) -> int:
    try:
        main(list(args))
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def last_result(runs: Path) -> dict:
    run_dir = sorted(runs.iterdir())[-1]
    return json.loads((run_dir / "result.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def capability(tmp_path_factory, meridian_url) -> str:
    out = tmp_path_factory.mktemp("caps")
    runs = tmp_path_factory.mktemp("runs")
    import yaml
    base = yaml.safe_load(Path("policy.yaml").read_text(encoding="utf-8"))
    base["allowed_origins"] = [meridian_url]
    base["handoff_timeout_s"] = 20
    pol = out / "policy.yaml"
    pol.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")
    code = run_cli("discover", CONTRACT, "--input", "member_id=100234", "--scripted", SCRIPT,
                   "--out", str(out), "--runs", str(runs), "--policy", str(pol))
    assert code == 0
    cap = out / "member_savings_balance.json"
    assert run_cli("approve", str(cap), "--by", "tests") == 0
    return str(cap)


def test_discovery_recorded_a_parameterized_flow(capability):
    cap = json.loads(Path(capability).read_text(encoding="utf-8"))
    assert cap["status"] == "approved"
    assert [s["action"] for s in cap["steps"]] == ["click", "type", "click"]
    assert cap["steps"][1]["value"] == {"param": "member_id"}
    assert cap["steps"][2]["expect"]["url"] == "/main/members/{member_id}"
    assert "100234" not in json.dumps(cap["steps"]), "concrete input values must not leak into steps"


def test_replay_success_for_a_different_member(capability, tmp_path, policy_file):
    code = run_cli("replay", capability, "--input", "member_id=101502", "--runs", str(tmp_path), "--policy", policy_file)
    assert code == 0
    r = last_result(tmp_path)
    assert r["status"] == "success"
    assert r["outputs"] == {"savings_balance": "12000.00", "member_name": "Elena Ruiz"}
    assert all(not s["drift"] for s in r["steps"])


def test_replay_reports_not_found_as_an_outcome(capability, tmp_path, policy_file):
    code = run_cli("replay", capability, "--input", "member_id=999999", "--runs", str(tmp_path), "--policy", policy_file)
    assert code == 3
    r = last_result(tmp_path)
    assert r["status"] == "outcome"
    assert r["outcome"]["code"] == "not_found"
    assert "999999" in r["outcome"]["message"]


def test_replay_refuses_bad_input_before_touching_the_app(capability, tmp_path, policy_file):
    code = run_cli("replay", capability, "--input", "member_id=12", "--runs", str(tmp_path), "--policy", policy_file)
    assert code == 1
    r = last_result(tmp_path)
    assert r["failure"]["code"] == "bad_input" and r["steps"] == []


def test_replay_recovers_from_session_expiry(capability, tmp_path, policy_file):
    code = run_cli("replay", capability, "--input", "member_id=100234", "--fault", "session_expired",
                   "--runs", str(tmp_path), "--policy", policy_file)
    assert code == 0
    r = last_result(tmp_path)
    assert r["status"] == "success"
    assert any("session_expired" in s["recoveries"] for s in r["steps"])


def test_replay_stops_on_application_error_with_evidence(capability, tmp_path, policy_file):
    code = run_cli("replay", capability, "--input", "member_id=100234", "--fault", "error",
                   "--runs", str(tmp_path), "--policy", policy_file)
    assert code == 1
    r = last_result(tmp_path)
    f = r["failure"]
    assert f["code"] == "fatal_condition" and f["condition_id"] == "app_error"
    assert (Path(r["run_dir"]) / f["evidence"]["screenshot"]).exists()
    assert (Path(r["run_dir"]) / f["evidence"]["snapshot"]).exists()
    assert "ORA-01555" in f["observed"]


def test_unknown_screen_goes_to_a_person_who_fixes_it_on_the_live_session(capability, tmp_path, policy_file):
    code = run_cli("replay", capability, "--input", "member_id=100234", "--fault", "surprise",
                   "--operator", "scripts/operator_dismiss_alert.json", "--cdp-port", "9444",
                   "--runs", str(tmp_path), "--policy", policy_file)
    assert code == 0
    r = last_result(tmp_path)
    assert r["status"] == "success"
    h = r["handoffs"][0]
    assert h["kind"] == "unknown_state" and h["resolution"] == "resumed"
    assert any("Remind Me Later" in a["detail"] for a in h["human_actions"])
    assert r["steps"][0]["status"] == "recovered"


def test_draft_capabilities_do_not_replay_unattended(tmp_path, policy_file, capability):
    cap = json.loads(Path(capability).read_text(encoding="utf-8"))
    cap["status"] = "draft"
    draft = tmp_path / "draft.json"
    draft.write_text(json.dumps(cap), encoding="utf-8")
    code = run_cli("replay", str(draft), "--input", "member_id=100234", "--runs", str(tmp_path / "r"), "--policy", policy_file)
    assert code == 1
    assert last_result(tmp_path / "r")["failure"]["code"] == "not_approved"
