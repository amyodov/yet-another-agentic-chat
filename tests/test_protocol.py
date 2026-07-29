"""Wire protocol: identifiers, serialization, and message shapes."""

import time

import pytest

from yaac import protocol

# Text that has broken naive implementations. Names are raw UTF-8 and are never
# parsed, split, validated, or case-folded, so all of these must survive intact.
AWKWARD_TEXT = pytest.mark.parametrize(
    "text",
    [
        "plain ascii",
        "forum with spaces",
        "café",
        "日本語",
        "emoji 🛰",
        'quotes "and" \\backslashes\\',
        "newline\nand\ttab",
        "",
        "  padded  ",
        "null",
        "yaac",
        "a" * 10_000,
    ],
    ids=[
        "ascii",
        "spaces",
        "accented",
        "cjk",
        "emoji",
        "punctuation",
        "whitespace",
        "empty",
        "padded",
        "looks-null",
        "looks-reserved",
        "long",
    ],
)


def test_ulid_properties():
    ulids = [protocol.new_ulid() for _ in range(1000)]
    assert len(set(ulids)) == 1000
    assert {len(u) for u in ulids} == {26}

    # The leading 10 characters are a 48-bit millisecond timestamp, so ordering
    # holds across milliseconds; within one, the rest is random and unordered.
    stamps = [u[:10] for u in ulids]
    assert stamps == sorted(stamps)

    earlier = protocol.new_ulid()
    time.sleep(0.005)
    assert protocol.new_ulid() > earlier


@AWKWARD_TEXT
def test_arbitrary_text_survives_the_wire_unchanged(text):
    destination = protocol.loads(protocol.dumps(protocol.destination(text, text)))
    assert destination == {"channel": text, "nickname": text}

    envelope = protocol.envelope(channel=text, sender=text, to=text, body=text)
    assert protocol.loads(protocol.dumps(envelope)) == envelope
    assert (envelope["channel"], envelope["from"], envelope["to"], envelope["body"]) == (
        text,
        text,
        text,
        text,
    )

    # No nickname is reserved, so text that looks like control traffic is not.
    assert protocol.is_control(envelope) is False


@pytest.mark.parametrize(
    "to,expected",
    [("bob", "bob"), (None, None)],
    ids=["direct", "broadcast"],
)
def test_envelope_records_how_it_was_addressed(to, expected):
    # Recipients must tell the two apart: answering privately to something
    # everyone heard, or the reverse, is a real failure mode.
    envelope = protocol.envelope(channel="forum", sender="ann", to=to, body="hi")
    assert envelope["to"] == expected
    assert envelope["from"] == "ann"
    assert envelope["channel"] == "forum"
    assert len(envelope["id"]) == 26
    assert envelope["ts"].endswith("Z")


@pytest.mark.parametrize(
    "message,expected",
    [
        (protocol.whois(), {"from": None, "kind": "whois"}),
        (
            protocol.roster("forum", ["ann", "bob"]),
            {"from": None, "kind": "roster", "channel": "forum", "peers": ["ann", "bob"]},
        ),
        (
            protocol.bounce("01J", "no such nickname on this channel"),
            {
                "from": None,
                "kind": "bounce",
                "id": "01J",
                "reason": "no such nickname on this channel",
            },
        ),
        (
            protocol.error("nickname taken on this channel"),
            {"from": None, "kind": "error", "reason": "nickname taken on this channel"},
        ),
        (
            protocol.channels([{"name": "forum", "uuid": "01J", "count": 2}]),
            {
                "from": None,
                "kind": "channels",
                "channels": [{"name": "forum", "uuid": "01J", "count": 2}],
            },
        ),
        (
            protocol.hello("forum", "ann", "01J"),
            {
                "from": None,
                "kind": "hello",
                "channel": "forum",
                "nickname": "ann",
                "reply_to": "01J",
            },
        ),
        (protocol.channels_query(), {"from": None, "kind": "channels?"}),
    ],
    ids=["whois", "roster", "bounce", "error", "channels", "hello", "channels?"],
)
def test_control_messages_have_exactly_the_documented_shape(message, expected):
    # Control traffic is identified by a null sender rather than a reserved
    # nickname, because a user may legitimately choose any string as one.
    assert message == expected
    assert protocol.is_control(message) is True
    assert protocol.loads(protocol.dumps(message)) == expected


@pytest.mark.parametrize(
    "frame",
    [b"", b"not json", b"{", b'{"unterminated": ', b"\xff\xfe not utf-8"],
    ids=["empty", "text", "truncated", "partial", "bad-utf8"],
)
def test_malformed_frames_raise_valueerror(frame):
    with pytest.raises(ValueError):
        protocol.loads(frame)


def test_dumps_emits_utf8_rather_than_escapes():
    assert "日本語".encode() in protocol.dumps({"n": "日本語"})
