"""Wire protocol: identifiers, serialization, and message shapes."""

import time

import pytest

from yaac import protocol
from yaac.protocol import Address, Destination, Envelope

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
    where = Destination.from_wire(protocol.loads(protocol.dumps(protocol.destination(text, Address(text, text)))))
    assert where == Destination(channel=text, to=Address(nickname=text, handle=text))

    sent = protocol.envelope(channel=text, sender=Address(text, "01H"), to=Address(text, "01T"), body=text)
    restored = Envelope.from_wire(protocol.loads(protocol.dumps(sent)))
    assert (restored.channel, restored.sender.nickname, restored.to.nickname, restored.body) == (
        text,
        text,
        text,
        text,
    )

    # No nickname is reserved, so text that looks like control traffic is not.
    assert protocol.is_control(sent) is False


@pytest.mark.parametrize(
    "to,expected",
    [(Address("bob", "01B"), {"handle": "01B", "nickname": "bob"}), (None, None)],
    ids=["direct", "broadcast"],
)
def test_envelope_records_how_it_was_addressed(to, expected):
    # Recipients must tell the two apart: answering privately to something everyone heard, or the reverse, reaches
    # the wrong people.
    sent = protocol.envelope(channel="forum", sender=Address("ann", "01A"), to=to, body="hi")
    assert sent["to"] == expected
    assert sent["from"] == {"handle": "01A", "nickname": "ann"}
    assert sent["channel"] == "forum"
    assert len(sent["id"]) == 26
    assert sent["ts"].endswith("Z")


@pytest.mark.parametrize(
    "message,expected",
    [
        (protocol.whois(), {"from": None, "kind": "whois"}),
        (
            protocol.roster("forum", [Address("ann", "01A"), Address("bob", "01B")]),
            {
                "from": None,
                "kind": "roster",
                "channel": "forum",
                "peers": [
                    {"handle": "01A", "nickname": "ann"},
                    {"handle": "01B", "nickname": "bob"},
                ],
            },
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


@AWKWARD_TEXT
def test_serialization_is_canonical_so_equal_content_gives_equal_bytes(text):
    """A byte-stable encoding is what a signature or content hash would be computed over, so insertion order must
    not be able to change the result."""
    fields = {"z": text, "a": {"n": 1, "m": text}, "body": text}
    shuffled = {"body": text, "a": {"m": text, "n": 1}, "z": text}
    assert protocol.dumps(fields) == protocol.dumps(shuffled)

    encoded = protocol.dumps(fields).decode("utf-8")
    assert encoded.index('"a"') < encoded.index('"body"') < encoded.index('"z"')  # sorted at the top level
    assert encoded.index('"m"') < encoded.index('"n"')  # and at every level below
    assert ", " not in encoded and ": " not in encoded  # no insignificant whitespace


def test_an_envelope_serializes_to_one_line_whatever_the_body_contains():
    body = 'first\nsecond\ttabbed\n\n"quoted" and \\backslash'
    line = protocol.dumps(protocol.envelope(channel="forum", sender=Address("ann"), to=None, body=body))
    assert line.count(b"\n") == 0  # newlines survive as escapes, so the JSONL framing holds
    assert Envelope.from_wire(protocol.loads(line)).body == body


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ({"nickname": "ann", "handle": "01A"}, Address("ann", "01A")),
        ({"nickname": "ann"}, Address("ann", None)),
        ({"handle": "01A"}, Address(None, "01A")),
        ({}, Address(None, None)),
    ],
    ids=["null", "both", "nickname-only", "handle-only", "empty"],
)
def test_addresses_accept_either_locator(value, expected):
    assert Address.from_wire(value) == expected


@pytest.mark.parametrize(
    "value",
    ["a bare string", 42, ["list"], {"nickname": 42}, {"handle": []}],
    ids=["string", "number", "list", "bad-nickname", "bad-handle"],
)
def test_a_malformed_address_is_rejected_rather_than_coerced(value):
    with pytest.raises(ValueError):
        Address.from_wire(value)
