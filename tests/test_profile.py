from pathlib import Path

from teller.profile import apply_overlay, load_profile


def test_overlay_overrides_base_url_and_prepends_conditions(tmp_path: Path):
    overlay = tmp_path / "tenant.yaml"
    overlay.write_text(
        "base_url: http://tenant.example:8080\n"
        "version: '4.2.9'\n"
        "conditions:\n"
        "  - id: session_expired\n"
        "    kind: recoverable\n"
        "    when: {url: '^/auth/expired'}\n"
        "    then: {kind: reauth}\n"
        "  - id: branch_closed\n"
        "    kind: outcome\n"
        "    outcome: branch_closed\n"
        "    when: {text: 'Branch is closed'}\n",
        encoding="utf-8")
    import os
    os.environ.pop("MERIDIAN_URL", None)
    p = load_profile("meridian", str(overlay))
    assert p.base_url == "http://tenant.example:8080"
    assert p.version == "4.2.9"
    ids = [c.id for c in p.conditions]
    assert ids[:2] == ["session_expired", "branch_closed"]
    assert ids.count("session_expired") == 1, "an overlay entry replaces the base entry with the same id"
    assert p.conditions[0].when.url == "^/auth/expired"
    assert p.auth is not None and p.main_frame == ["main"], "everything else comes from the base profile"


def test_apply_overlay_merges_auth():
    base = {"id": "x", "auth": {"login_url": "/login", "user_env": "U"}, "conditions": []}
    out = apply_overlay(base, {"auth": {"login_url": "/signin"}})
    assert out["auth"] == {"login_url": "/signin", "user_env": "U"}
