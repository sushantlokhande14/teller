from teller.conditions import condition_message, detector_matches, find_condition
from teller.profile import load_profile
from teller.schema import Condition, Detector, Handler
from teller.surface.base import Observation


def _obs(path="/main/members", text="", heading=None, status=200) -> Observation:
    return Observation(url="http://x" + path, path=path, title="", heading=heading, text=text, elements=[],
                       http_status=status, frames=[], taken_at="now")


def test_detector_all_fields_must_match():
    d = Detector(url="^/login", text="expired")
    assert detector_matches(d, _obs("/login?next=/x", "Your session has expired"))
    assert not detector_matches(d, _obs("/login", "welcome"))
    assert not detector_matches(d, _obs("/main/home", "expired"))
    assert detector_matches(Detector(http_status=500), _obs(status=500))


def test_message_extraction_prefers_group_one():
    c = Condition(id="nf", kind="outcome", outcome="not_found", when=Detector(text="No member found"),
                  message=r"(No member found for number [^.]*\.)")
    assert condition_message(c, _obs(text="Lookup No member found for number 999. Try again")) == "No member found for number 999."


def test_find_condition_is_ordered():
    first = Condition(id="specific", kind="outcome", outcome="frozen", when=Detector(text="Frozen"))
    second = Condition(id="generic", kind="recoverable", when=Detector(text="Fro"), then=Handler(kind="wait", wait_ms=1))
    hit = find_condition([first, second], _obs(text="Status Frozen"))
    assert hit and hit[0].id == "specific"


def test_meridian_profile_taxonomy_against_real_screen_text():
    profile = load_profile("meridian")
    by_id = {c.id: c for c in profile.conditions}
    assert by_id["session_expired"].kind == "recoverable"
    assert find_condition(profile.conditions, _obs("/login?next=/main/members&reason=timeout", "SIGN IN"))[0].id == "session_expired"
    assert find_condition(profile.conditions, _obs(text="Member Lookup No member found for number 42."))[0].outcome == "not_found"
    assert find_condition(profile.conditions, _obs(text="Access Denied You do not have permission to view Reports."))[0].outcome == "permission_denied"
    assert find_condition(profile.conditions, _obs(text="Application Error ... Reference: ORA-01555 snapshot too old."))[0].kind == "fatal"
    # a password expiry warning must not be mistaken for a validation error
    assert find_condition(profile.conditions, _obs(text="Security Alert Passwords must be changed every 90 days")) is None
