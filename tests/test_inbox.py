"""The local inbox: append-only log, cursor, and cleanup."""

import pytest

from yaac.inbox import Inbox


@pytest.fixture
def box(isolated_runtime):
    inbox = Inbox("01TESTHANDLE")
    inbox.create({"handle": "01TESTHANDLE", "channel": "forum", "nickname": "ann"})
    return inbox


def test_lifecycle_creates_then_removes_every_file(box, isolated_runtime):
    assert [box.log_path.exists(), box.cursor_path.exists(), box.live_path.exists()] == [
        True,
        True,
        True,
    ]
    box.append({"body": "something"})

    box.destroy()
    assert [p for p in isolated_runtime.rglob("*") if p.is_file()] == []


def test_the_cursor_delivers_each_message_exactly_once(box):
    box.append({"body": "first"})
    box.append({"body": "second"})

    assert [m["body"] for m in box.read_new()] == ["first", "second"]
    assert box.read_new() == []

    box.append({"body": "third"})
    assert box.pending_count() == 1
    assert box.pending_count() == 1  # peeking must not consume
    assert [m["body"] for m in box.read_new()] == ["third"]
    assert box.pending_count() == 0

    # The cursor moves but the log is never truncated: it is the project's
    # primary debugging tool, and must stay complete for `tail -f`.
    assert len(box.log_path.read_text(encoding="utf-8").strip().splitlines()) == 3


@pytest.mark.parametrize(
    "body",
    ["café", "日本語", "emoji 🛰", 'quotes "and" \\slashes\\', "a" * 50_000],
    ids=["accented", "cjk", "emoji", "punctuation", "large"],
)
def test_bodies_survive_the_log_unchanged(box, body):
    box.append({"body": body})
    assert [m["body"] for m in box.read_new()] == [body]


def test_a_torn_trailing_line_waits_for_the_rest_of_itself(box):
    box.append({"body": "complete"})
    with box.log_path.open("a", encoding="utf-8") as f:
        f.write('{"body": "half-writ')

    assert [m["body"] for m in box.read_new()] == ["complete"]

    with box.log_path.open("a", encoding="utf-8") as f:
        f.write('ten"}\n')
    assert [m["body"] for m in box.read_new()] == ["half-written"]


def test_a_corrupt_line_does_not_wedge_the_inbox(box):
    with box.log_path.open("a", encoding="utf-8") as f:
        f.write("this is not json at all\n")
    box.append({"body": "after the corruption"})

    assert [m["body"] for m in box.read_new()] == ["after the corruption"]


def test_reading_an_inbox_that_was_never_created_is_harmless(isolated_runtime):
    never = Inbox("01NEVERCREATED")
    assert [never.read_new(), never.pending_count()] == [[], 0]


def test_lines_carry_the_magic_number_and_a_stable_field_order(box):
    """A reader must be able to identify a YAAC line, and the version that wrote it, from its first bytes; and
    equal content must give equal bytes so a line has one identity."""
    box.append({"channel": "forum", "id": "01A", "body": "text"})
    box.append({"body": "text", "id": "01A", "channel": "forum"})

    first, second = box.log_path.read_text(encoding="utf-8").splitlines()
    assert first == second
    assert first == '{"yaac":1,"id":"01A","channel":"forum","body":"text"}'


def test_a_multiline_body_still_occupies_exactly_one_line(box):
    body = "first\nsecond\n\nfourth"
    box.append({"body": body})
    assert len(box.log_path.read_text(encoding="utf-8").splitlines()) == 1
    assert [m["body"] for m in box.read_new()] == [body]
