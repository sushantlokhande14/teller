from teller.policy import Policy, Redactor, RiskyPolicy


def _policy(mode="confirm") -> Policy:
    return Policy(allowed_origins=["http://127.0.0.1:5057"], blocked_paths=["^/logout", "^/__"],
                  risky=RiskyPolicy(mode=mode, button_names=["(?i)^(confirm|transfer)"], url_patterns=["/open$"]))


def test_origin_allowlist():
    p = _policy()
    assert p.url_allowed("http://127.0.0.1:5057/main/members")[0]
    ok, why = p.url_allowed("http://evil.example/main/members")
    assert not ok and "origin" in why
    ok, why = p.url_allowed("http://127.0.0.1:5057/logout")
    assert not ok and "blocked" in why


def test_navigation_target_is_checked_too():
    p = _policy()
    v = p.check("navigate", "http://127.0.0.1:5057/main/home", target_url="http://elsewhere.example/x")
    assert not v.allowed


def test_action_allowlist():
    p = _policy()
    p.allowed_actions = ["click"]
    assert not p.check("type", "http://127.0.0.1:5057/main/home").allowed


def test_risk_from_button_name_and_form_action():
    p = _policy()
    assert p.classify("click", "Search") == "safe"
    assert p.classify("click", "Confirm Open") == "risky"
    assert p.classify("click", "Review", href_or_form="/main/members/1/subaccounts/open") == "risky"
    assert p.classify("type", "Confirm", recorded="safe") == "safe", "typing is never risky by name"
    assert p.classify("click", "Anything", recorded="risky") == "risky", "a recorded risk level sticks"


def test_risky_modes():
    v = _policy("confirm").check("click", "http://127.0.0.1:5057/x", name="Confirm Open")
    assert v.allowed and v.needs_confirmation and v.risk == "risky"
    v = _policy("block").check("click", "http://127.0.0.1:5057/x", name="Confirm Open")
    assert not v.allowed
    v = _policy("allow").check("click", "http://127.0.0.1:5057/x", name="Confirm Open")
    assert v.allowed and not v.needs_confirmation


def test_shipped_redaction_patterns_do_not_eat_run_ids():
    import yaml
    patterns = yaml.safe_load(open("policy.yaml", encoding="utf-8"))["redaction"]["patterns"]
    r = Redactor(patterns)
    assert r.text("runs/replay-20260912-072944-23f6/screens/failure-s1.png") == "runs/replay-20260912-072944-23f6/screens/failure-s1.png"
    assert r.text("card 4111 1111 1111 1111 on file") == "card [redacted:card] on file"
    assert r.text("card 4111111111111111 on file") == "card [redacted:card] on file"
    assert r.text("SSN 123-45-6789") == "SSN [redacted:ssn]"


def test_redactor_scrubs_secrets_and_patterns():
    r = Redactor({"ssn": r"\b\d{3}-\d{2}-\d{4}\b"}, {"credential": "teller!23", "ssn_input": "123-45-6789"})
    out = r.any({"note": "signed in with teller!23", "nested": ["SSN 987-65-4321", {"k": "123-45-6789"}]})
    assert out["note"] == "signed in with [redacted:credential]"
    assert out["nested"][0] == "SSN [redacted:ssn]"
    assert out["nested"][1]["k"] == "[redacted:ssn_input]"
    assert r.text("nothing here") == "nothing here"
