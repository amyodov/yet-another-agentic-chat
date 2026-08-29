"""Wire protocol: identifiers, serialization, and message shapes."""

import json
import time
from typing import Any

import pytest

from yaac import protocol
from yaac.protocol import MAGIC, PROTOCOL_VERSION, Address, Envelope, Scope

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
    """Channel and participant names are user-chosen UTF-8 and pass through untouched, and so does a body that
    happens to look like operator mail -- what makes the hat read a message is who it is addressed to, never what
    it says."""
    sent = protocol.message(
        Scope(channel=text, peer=Address(text, "01T")),
        frm=Scope(channel=text, peer=Address(text, "01H")),
        body=text,
        tags=(text,),
    )
    restored = Envelope.from_wire(protocol.loads(protocol.dumps(sent.to_wire())))
    assert restored == sent
    assert (restored.to.channel, restored.to.peer.name, restored.frm.peer.name, restored.body) == (
        text,
        text,
        text,
        text,
    )
    assert restored.for_the_hat is False


@pytest.mark.parametrize(
    "peer,expected",
    [
        (Address("Bob", "01B"), {"channel": "forum", "peer": {"name": "Bob", "zmq_routing_id": "01B"}}),
        (None, {"channel": "forum"}),
    ],
    ids=["whisper", "broadcast"],
)
def test_a_message_records_how_it_was_addressed(peer: Address | None, expected: Any) -> None:
    """Recipients must tell the two apart: answering privately to something everyone heard, or the reverse,
    reaches the wrong people."""
    sent = protocol.message(
        Scope(channel="forum", peer=peer), frm=Scope(channel="forum", peer=Address("Alice", "01A")), body="hi"
    )
    wire = sent.to_wire()
    assert wire["to"] == expected
    assert wire["from"] == {"channel": "forum", "peer": {"name": "Alice", "zmq_routing_id": "01A"}}
    assert len(wire["id"]) == 26
    assert wire["ts"].endswith("Z")


def test_mentions_and_tags_ride_beside_the_addressing_not_inside_the_message() -> None:
    """Which is what lets the hat complete a mention from its own table without reading a body: rule 5 is about
    what a message says, and these say who is called on rather than what was said."""
    sent = protocol.message(
        Scope(channel="forum"),
        body="who owns the migration?",
        mentions=(Address("Bob"), Address("Carol", "01C")),
        tags=("build", "urgent"),
    )
    wire = sent.to_wire()
    assert wire["mentions"] == [{"name": "Bob"}, {"name": "Carol", "zmq_routing_id": "01C"}]
    assert wire["tags"] == ["build", "urgent"]
    assert Envelope.from_wire(wire) == sent
    # Nothing empty is written: a message with no mentions carries no key for them.
    assert "mentions" not in protocol.message(Scope(channel="forum"), body="hi").to_wire()


PEER = Scope(peer=Address("Alice", "01A"))


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            protocol.hello("forum", "Alice", "01J"),
            {"op": "hello", "payload": {"channel": "forum", "name": "Alice", "reply_to": "01J"}},
        ),
        (protocol.channels_query(), {"op": "channels"}),
    ],
    ids=["hello", "channels?"],
)
def test_mail_to_the_hat_is_addressed_to_nobody(message: protocol.Envelope, expected: dict) -> None:
    """A question for the operator is `to: {}` and carries no `from` at all -- senders never write one. What
    makes the hat read this rather than carry it is the addressing, which is hard rule 5 restated."""
    wire = message.to_wire()
    assert message.for_the_hat is True
    assert wire["to"] == {}
    assert "from" not in wire
    assert {k: v for k, v in wire.items() if k in ("op", "payload")} == expected


@pytest.mark.parametrize(
    "message,expected",
    [
        (protocol.whois(to=PEER), {"op": "whois"}),
        (
            protocol.roster("forum", [Address("Alice", "01A"), Address("Bob", "01B")], to=PEER),
            {
                "op": "roster",
                "payload": {
                    "channel": "forum",
                    "members": [{"name": "Alice", "zmq_routing_id": "01A"}, {"name": "Bob", "zmq_routing_id": "01B"}],
                },
            },
        ),
        (
            protocol.bounce("01J", "no such recipient on this channel", to=PEER),
            {"op": "bounce", "payload": {"id": "01J", "reason": "no such recipient on this channel"}},
        ),
        (
            protocol.error("name taken on this channel", to=PEER),
            {"op": "error", "payload": {"reason": "name taken on this channel"}},
        ),
        (
            protocol.channels([{"name": "forum", "uuid": "01J", "count": 2}], to=PEER),
            {"op": "channels", "payload": {"channels": [{"name": "forum", "uuid": "01J", "count": 2}]}},
        ),
    ],
    ids=["whois", "roster", "bounce", "error", "channels"],
)
def test_the_hat_answers_as_itself(message: protocol.Envelope, expected: dict) -> None:
    """`from: {}` is the operator speaking rather than carrying somebody, and it is unforgeable because a sender
    never writes `from` at all -- the hat stamps every one it relays.

    Direction is what tells a question from its answer: `to: {}` asked, `from: {}` replies, and `op` is the same
    word in both."""
    wire = message.to_wire()
    assert wire["from"] == {}
    assert message.for_the_hat is False  # it is addressed to a participant, not to the operator
    assert {k: v for k, v in wire.items() if k in ("op", "payload")} == expected
    # dumps stamps the version, so what comes back is the message plus its magic field.
    assert protocol.loads(protocol.dumps(wire)) == {"yaac": PROTOCOL_VERSION, **wire}


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
        return protocol.message(
            Scope(channel=text), frm=Scope(channel=text, peer=Address(text, "01A")), body=text, msg_id="01M"
        ).to_wire()

    first, second = protocol.dumps(build()), protocol.dumps(build())
    assert first == second

    encoded = first.decode("utf-8")
    assert ", " not in encoded and ": " not in encoded  # no insignificant whitespace


@pytest.mark.parametrize(
    "message",
    [
        protocol.message(Scope(channel="forum"), body="hi"),
        protocol.whois(to=PEER),
        protocol.roster("forum", [Address("Alice", "01A")], to=PEER),
        protocol.bounce("01J", "gone", to=PEER),
        protocol.error("refused", to=PEER),
        protocol.channels([{"name": "forum", "uuid": "01J", "count": 1}], to=PEER),
        protocol.hello("forum", "Alice", "01J"),
        protocol.channels_query(),
    ],
    ids=["chat", "whois", "roster", "bounce", "error", "channels", "hello", "channels?"],
)
def test_every_message_begins_with_the_magic_number(message: protocol.Envelope) -> None:
    """A reader can identify a YAAC message, and the version that wrote it, from the first bytes alone."""
    encoded = protocol.dumps(message.to_wire())
    assert encoded.startswith(MAGIC)
    assert encoded.startswith(protocol.MAGIC_PREFIX)
    assert protocol.parse(encoded)["yaac"] == PROTOCOL_VERSION


def test_the_magic_claims_no_trailing_comma() -> None:
    """A message carrying nothing but the version would end right after it, so the comma cannot be promised."""
    assert MAGIC == b'{"yaac":2'
    assert protocol.dumps({}) == b'{"yaac":2}'
    assert protocol.dumps({}).startswith(MAGIC)


@pytest.mark.parametrize(
    "message",
    [{}, {"yaac": 3}, {"yaac": 1}, {"yaac": "2"}, {"yaac": None}, {"yaac": 2.0}, {"yaac": True}],
    ids=["missing", "newer", "version-1", "string", "null", "float", "bool"],
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
        protocol.message(
            Scope(channel="forum", peer=Address("Bob", "01B")),
            frm=Scope(channel="forum", peer=Address("Alice", "01A")),
            body="x" * 5000,
        ).to_wire()
    ).decode("utf-8")
    assert encoded.index('"yaac"') == 1
    # body is the only unbounded field, so everything routing-related precedes it.
    assert max(encoded.index(f'"{f}"') for f in ("id", "ts", "channel", "from", "to")) < encoded.index('"body"')


def test_an_envelope_serializes_to_one_line_whatever_the_body_contains() -> None:
    body = 'first\nsecond\ttabbed\n\n"quoted" and \\backslash'
    line = protocol.dumps(protocol.message(Scope(channel="forum"), body=body).to_wire())
    assert line.count(b"\n") == 0  # newlines survive as escapes, so the JSONL framing holds
    assert Envelope.from_wire(protocol.loads(line)).body == body


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ({"name": "ann", "zmq_routing_id": "01A"}, Address("ann", "01A")),
        ({"name": "ann"}, Address("ann", None)),
        ({"zmq_routing_id": "01A"}, Address(None, "01A")),
    ],
    ids=["null", "both", "name-only", "routing_id-only"],
)
def test_addresses_accept_either_locator(value: Any, expected: Any) -> None:
    assert Address.from_wire(value) == expected
    if expected is not None:
        # What it does not have, it does not write: the shorter wire, and the same rule scopes follow.
        assert expected.to_wire() == value


@pytest.mark.parametrize(
    "value",
    ["a bare string", 42, ["list"], {"name": 42}, {"zmq_routing_id": []}, {}, {"name": None}],
    ids=["string", "number", "list", "bad-name", "bad-routing_id", "nobody", "null-locator"],
)
def test_a_malformed_address_is_rejected_rather_than_coerced(value: Any) -> None:
    """`{}` and a null locator are refused for the reason a null scope field is: nobody is said by leaving the
    field out, and an address that names nobody is a message addressed to nowhere."""
    with pytest.raises(ValueError):
        Address.from_wire(value)


# Scopes -----------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,wire",
    [
        (Scope(), {}),
        (Scope(channel="forum"), {"channel": "forum"}),
        (Scope(peer=Address("Bob")), {"peer": {"name": "Bob"}}),
        (
            Scope(channel="forum", peer=Address("Bob")),
            {"channel": "forum", "peer": {"name": "Bob"}},
        ),
    ],
    ids=["hat", "channel", "peer", "channel-and-peer"],
)
def test_a_scope_carries_exactly_the_fields_it_was_given(scope: Scope, wire: dict) -> None:
    """One spelling out, so equal content gives equal bytes."""
    assert scope.to_wire() == wire
    assert Scope.from_wire(wire) == scope


def test_the_hat_is_addressed_one_way_and_the_synonyms_are_refused() -> None:
    """One concept, one encoding. `null` and `{"channel": null}` are what a reader would understand perfectly
    well, which is why they are refused rather than accepted: a format that takes synonyms has to keep answering
    which of them is canonical, and every reader that guesses differently is a bug waiting for a peer that
    disagrees.

    It leaves "everybody on the unnamed channel" without an encoding, deliberately: if the world channel is ever
    built it says so with a marker of its own, rather than by an absence a serializer could drop.
    """
    assert Scope.from_wire({}).is_the_hat
    assert Scope().to_wire() == {}
    for synonym in (None, {"channel": None}, {"peer": None}, {"channel": None, "peer": None}):
        with pytest.raises(ValueError):
            Scope.from_wire(synonym)


def test_a_scope_ignores_a_field_it_does_not_know() -> None:
    """The other half of strictness: refusing an unknown field would make every added locator a breaking change,
    and the format promises the opposite."""
    assert Scope.from_wire({"channel": "forum", "galaxy": "andromeda"}) == Scope(channel="forum")


@pytest.mark.parametrize(
    "value",
    ["a string", 42, [], {"channel": 42}, {"peer": "Bob"}],
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
