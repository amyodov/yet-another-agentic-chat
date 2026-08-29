"""Wire protocol: identifiers, serialization, and message shapes."""

import json
import time
from typing import Any

import pytest

from yaac import protocol
from yaac.protocol import MAGIC, PROTOCOL_VERSION, Address, Destination, Envelope, Scope

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


def test_ulid_properties() -> None:
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
def test_arbitrary_text_survives_the_wire_unchanged(text: str) -> None:
    where = Destination.from_wire(protocol.loads(protocol.dumps(protocol.destination(text, Address(text, text)))))
    assert where == Destination(channel=text, to=Address(name=text, routing_id=text))

    sent = protocol.envelope(channel=text, sender=Address(text, "01H"), to=Address(text, "01T"), body=text)
    restored = Envelope.from_wire(protocol.loads(protocol.dumps(sent)))
    assert (restored.channel, restored.sender.name, restored.to.name, restored.body) == (
        text,
        text,
        text,
        text,
    )

    # No name is reserved, so text that looks like control traffic is not.
    assert protocol.is_control(sent) is False


@pytest.mark.parametrize(
    "to,expected",
    [(Address("bob", "01B"), {"name": "bob", "zmq_routing_id": "01B"}), (None, None)],
    ids=["direct", "broadcast"],
)
def test_envelope_records_how_it_was_addressed(to: Address | None, expected: Any) -> None:
    # Recipients must tell the two apart: answering privately to something everyone heard, or the reverse, reaches
    # the wrong people.
    sent = protocol.envelope(channel="forum", sender=Address("ann", "01A"), to=to, body="hi")
    assert sent["to"] == expected
    assert sent["from"] == {"name": "ann", "zmq_routing_id": "01A"}
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
                    {"name": "ann", "zmq_routing_id": "01A"},
                    {"name": "bob", "zmq_routing_id": "01B"},
                ],
            },
        ),
        (
            protocol.bounce("01J", "no such name on this channel"),
            {
                "from": None,
                "kind": "bounce",
                "id": "01J",
                "reason": "no such name on this channel",
            },
        ),
        (
            protocol.error("name taken on this channel"),
            {"from": None, "kind": "error", "reason": "name taken on this channel"},
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
                "name": "ann",
                "reply_to": "01J",
            },
        ),
        (protocol.channels_query(), {"from": None, "kind": "channels?"}),
    ],
    ids=["whois", "roster", "bounce", "error", "channels", "hello", "channels?"],
)
def test_control_messages_have_exactly_the_documented_shape(message: dict[str, Any], expected: Any) -> None:
    # Control traffic is identified by a null sender rather than a reserved name, because a user may
    # legitimately choose any string as one.
    assert message == expected
    assert protocol.is_control(message) is True
    # dumps stamps the version, so what comes back is the message plus its magic field.
    assert protocol.loads(protocol.dumps(message)) == {"yaac": PROTOCOL_VERSION, **expected}


@pytest.mark.parametrize(
    "frame",
    [b"", b"not json", b"{", b'{"unterminated": ', b"\xff\xfe not utf-8"],
    ids=["empty", "text", "truncated", "partial", "bad-utf8"],
)
def test_malformed_frames_raise_valueerror(frame: bytes) -> None:
    with pytest.raises(ValueError):
        protocol.loads(frame)


def test_dumps_emits_utf8_rather_than_escapes() -> None:
    assert "日本語".encode() in protocol.dumps({"n": "日本語"})


@AWKWARD_TEXT
def test_equal_messages_serialize_to_equal_bytes(text: str) -> None:
    """A message has one identity, which is what a hash or signature would be computed over."""

    def build():
        return protocol.envelope(channel=text, sender=Address(text, "01A"), to=None, body=text, msg_id="01M")

    first, second = protocol.dumps(build()), protocol.dumps(build())
    assert first == second

    encoded = first.decode("utf-8")
    assert ", " not in encoded and ": " not in encoded  # no insignificant whitespace


@pytest.mark.parametrize(
    "message",
    [
        protocol.envelope(channel="forum", sender=Address("ann"), to=None, body="hi"),
        protocol.whois(),
        protocol.roster("forum", [Address("ann", "01A")]),
        protocol.bounce("01J", "gone"),
        protocol.error("refused"),
        protocol.channels([{"name": "forum", "uuid": "01J", "count": 1}]),
        protocol.hello("forum", "ann", "01J"),
        protocol.channels_query(),
        protocol.destination("forum", Address("bob")),
    ],
    ids=["envelope", "whois", "roster", "bounce", "error", "channels", "hello", "channels?", "destination"],
)
def test_every_message_begins_with_the_magic_number(message: dict[str, Any]) -> None:
    """A reader can identify a YAAC message, and the version that wrote it, from the first bytes alone."""
    encoded = protocol.dumps(message)
    assert encoded.startswith(MAGIC)
    assert encoded.startswith(protocol.MAGIC_PREFIX)
    assert protocol.parse(encoded)["yaac"] == PROTOCOL_VERSION


def test_the_magic_claims_no_trailing_comma() -> None:
    """A message carrying nothing but the version would end right after it, so the comma cannot be promised."""
    assert MAGIC == b'{"yaac":1'
    assert protocol.dumps({}) == b'{"yaac":1}'
    assert protocol.dumps({}).startswith(MAGIC)


@pytest.mark.parametrize(
    "message",
    [{}, {"yaac": 2}, {"yaac": 0}, {"yaac": "1"}, {"yaac": None}, {"yaac": 1.0}, {"yaac": True}],
    ids=["missing", "newer", "older", "string", "null", "float", "bool"],
)
def test_a_frame_this_build_cannot_read_is_rejected(message: dict[str, Any]) -> None:
    """Validation reads the parsed field rather than the leading bytes: that is what the format guarantees."""
    frame = json.dumps(message).encode("utf-8")
    with pytest.raises(ValueError, match="unsupported protocol version"):
        protocol.parse(frame)


@pytest.mark.parametrize(
    "frame",
    [b'"a string"', b"[1,2]", b"42", b"null"],
    ids=["string", "list", "number", "null"],
)
def test_a_frame_that_is_not_an_object_is_rejected(frame: bytes) -> None:
    with pytest.raises(ValueError):
        protocol.parse(frame)


def test_the_version_leads_and_the_body_trails() -> None:
    """`head -c` on a log must show the routing of every message, however long the bodies are."""
    encoded = protocol.dumps(
        protocol.envelope(channel="forum", sender=Address("ann", "01A"), to=Address("bob", "01B"), body="x" * 5000)
    ).decode("utf-8")
    assert encoded.index('"yaac"') == 1
    # body is the only unbounded field, so everything routing-related precedes it.
    assert max(encoded.index(f'"{f}"') for f in ("id", "ts", "channel", "from", "to")) < encoded.index('"body"')


def test_an_envelope_serializes_to_one_line_whatever_the_body_contains() -> None:
    body = 'first\nsecond\ttabbed\n\n"quoted" and \\backslash'
    line = protocol.dumps(protocol.envelope(channel="forum", sender=Address("ann"), to=None, body=body))
    assert line.count(b"\n") == 0  # newlines survive as escapes, so the JSONL framing holds
    assert Envelope.from_wire(protocol.loads(line)).body == body


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ({"name": "ann", "zmq_routing_id": "01A"}, Address("ann", "01A")),
        ({"name": "ann"}, Address("ann", None)),
        ({"zmq_routing_id": "01A"}, Address(None, "01A")),
        ({}, Address(None, None)),
    ],
    ids=["null", "both", "name-only", "routing_id-only", "empty"],
)
def test_addresses_accept_either_locator(value: Any, expected: Any) -> None:
    assert Address.from_wire(value) == expected


@pytest.mark.parametrize(
    "value",
    ["a bare string", 42, ["list"], {"name": 42}, {"zmq_routing_id": []}],
    ids=["string", "number", "list", "bad-name", "bad-routing_id"],
)
def test_a_malformed_address_is_rejected_rather_than_coerced(value: Any) -> None:
    with pytest.raises(ValueError):
        Address.from_wire(value)


# Scopes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,wire",
    [
        (Scope(), {}),
        (Scope(channel="forum"), {"channel": "forum"}),
        (Scope(peer=Address("Bob")), {"peer": {"name": "Bob", "zmq_routing_id": None}}),
        (
            Scope(channel="forum", peer=Address("Bob")),
            {"channel": "forum", "peer": {"name": "Bob", "zmq_routing_id": None}},
        ),
    ],
    ids=["hat", "channel", "peer", "channel-and-peer"],
)
def test_a_scope_carries_exactly_the_fields_it_was_given(scope: Scope, wire: dict) -> None:
    """One spelling out, so equal content gives equal bytes."""
    assert scope.to_wire() == wire
    assert Scope.from_wire(wire) == scope


@pytest.mark.parametrize(
    "value",
    [None, {}, {"channel": None}, {"peer": None}, {"channel": None, "peer": None}],
    ids=["null", "empty", "null-channel", "null-peer", "both-null"],
)
def test_every_spelling_of_empty_addresses_the_hat(value: Any) -> None:
    """An absence is an absence however it was written, and nothing is served by making a reader tell four
    spellings apart. Writing stays canonical -- `{}` is the only one this ever emits -- so a forgiving parser
    costs the format nothing.

    It leaves "everybody on the unnamed channel" without an encoding, deliberately: if the world channel is ever
    built it says so with a marker of its own, rather than by an absence a serializer could drop.
    """
    assert Scope.from_wire(value).is_the_hat
    assert Scope.from_wire(value).to_wire() == {}


@pytest.mark.parametrize(
    "value", ["a string", 42, [], {"channel": 42}, {"peer": "Bob"}],
    ids=["string", "number", "list", "bad-channel", "bad-peer"],
)
def test_a_malformed_scope_is_rejected_rather_than_coerced(value: Any) -> None:
    with pytest.raises(ValueError):
        Scope.from_wire(value)


@pytest.mark.parametrize(
    "frame,expected",
    [
        (b'{"yaac":1,"id":"x"}', 1),
        (b'{"yaac":17}', 17),
        (b'{"yaac":"one"}', None),
        (b"not a yaac message", None),
        (b"", None),
    ],
    ids=["current", "future", "not-a-number", "foreign", "empty"],
)
def test_a_version_can_be_read_off_a_frame_that_cannot_be_parsed(frame: bytes, expected: int | None) -> None:
    """For error paths. A peer that speaks another version answers nothing at all, and the session waiting on it
    sees a rendezvous point that accepts connections and never replies -- which reads as an empty network rather
    than a mismatch. Naming the version is what turns that into a sentence somebody can act on."""
    assert protocol.peek_version(frame) == expected


def test_the_refusal_names_both_versions() -> None:
    """A refusal that does not say what it saw and what it speaks leaves the reader with the empty-net symptom."""
    with pytest.raises(ValueError) as refusal:
        protocol.parse(b'{"yaac":99,"id":"x"}')
    assert "99" in str(refusal.value)
    assert str(PROTOCOL_VERSION) in str(refusal.value)
