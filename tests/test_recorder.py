from teller.recorder import Canon, TraceEntry, choose_row_key, derive_checkpoint, make_target
from teller.schema import ParamSpec
from teller.surface.base import Element, Observation


def _obs(path, heading=None, elements=()):
    return Observation(url="http://x" + path, path=path, title="", heading=heading, text="", elements=list(elements),
                       http_status=200, frames=[["main"]], taken_at="now")


def _cell(ref, name, row_cells, col_header, row_label=None):
    return Element(ref=ref, role="cell", name=name, frame=["main"], bbox=(0, 0, 10, 10), tag="td",
                   row_label=row_label or row_cells[0], row_cells=row_cells, col_header=col_header)


CANON = Canon({"member_id": "100234", "nickname": "Vacation fund"}, {"member_id": ParamSpec(), "nickname": ParamSpec()})


def test_canon_replaces_values_longest_first():
    assert CANON("/main/members/100234") == "/main/members/{member_id}"
    assert CANON("Vacation fund 100234") == "{nickname} {member_id}"
    assert CANON.value_for("100234").param == "member_id"
    assert CANON.value_for("other").literal == "other"


def test_canon_url_wildcards_server_generated_segments():
    assert CANON.url("/main/members/100234") == "/main/members/{member_id}"
    assert CANON.url("/main/members/100234/subaccounts/100234-S02") == "/main/members/{member_id}/subaccounts/*"
    assert CANON.url("/main/orders/8817?tab=2") == "/main/orders/*?tab=2"
    assert CANON.url("/main/home") == "/main/home"


def test_row_key_is_unique_alphabetic_cell():
    rows = [["100234-S01", "Savings", "Open", "$4,812.55", "$4,812.55"],
            ["100234-C01", "Checking", "Open", "$1,203.10", "$1,153.10"]]
    elements = [_cell(i, cells[3], cells, "Current Balance") for i, cells in enumerate(rows)]
    obs = _obs("/m", elements=elements)
    assert choose_row_key(elements[0], obs, CANON) == "Savings"  # "Open" is in both rows, so it is skipped
    assert choose_row_key(elements[1], obs, CANON) == "Checking"


def test_make_target_orders_locators_by_where_the_name_came_from():
    field = Element(ref=1, role="textbox", name="Member Number", frame=["main"], bbox=(0, 0, 1, 1), tag="input",
                    name_source="row", row_label="Member Number", css="td > input")
    t = make_target(field, _obs("/m"), CANON)
    assert [l.strategy for l in t.locators] == ["row_label", "role", "css", "bbox"]

    link = Element(ref=2, role="link", name="Member Lookup", frame=["nav"], bbox=(0, 0, 1, 1), tag="a",
                   name_source="content", row_label="Member Lookup", css="a")
    t = make_target(link, _obs("/m"), CANON)
    assert [l.strategy for l in t.locators] == ["role", "text", "css", "bbox"]

    value = _cell(3, "Priya Natarajan", ["Name", "Priya Natarajan"], "", row_label="Name")
    t = make_target(value, _obs("/m"), CANON)
    assert [l.strategy for l in t.locators][:1] == ["row_label"]
    assert all(l.strategy != "text" for l in t.locators), "a data value must not become a text locator"


def test_checkpoint_from_navigation_and_from_typing():
    before = _obs("/main/members", "Member Lookup")
    after = _obs("/main/members/100234", "Member Profile")
    click = TraceEntry(seq=1, tool="click", args={}, before=before, after=after)
    cp = derive_checkpoint(click, None, CANON, 5000)
    assert cp.url == "/main/members/{member_id}" and cp.heading == "Member Profile"

    typed = TraceEntry(seq=2, tool="type", args={"text": "100234"}, before=before, after=before)
    cp = derive_checkpoint(typed, None, CANON, 5000)
    assert cp.url == "/main/members" and cp.heading is None
