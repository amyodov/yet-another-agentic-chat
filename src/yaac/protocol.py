"""Wire protocol: addresses, envelopes, control messages, and canonical serialization.

Everything on the wire is JSON. Two kinds of message travel between a participant and the hat:

* **data** -- a participant sends `[destination JSON][body]`; the hat delivers `[envelope JSON]`.
* **control** -- a single JSON frame with a `kind` field.

Control messages are told apart from envelopes by carrying `"from": null` rather than by a reserved name or
channel name. Users may choose any string as a name, so the protocol reserves none.

Two properties are deliberate and depended on elsewhere:

**Every message starts with the same nine bytes.** `dumps` stamps `yaac: PROTOCOL_VERSION` ahead of every other
field, so a message always opens with `{"yaac":1` -- a magic number identifying the format and the version that
wrote it, without parsing. No trailing comma is promised: a message carrying no other field ends right there.

Receivers check the parsed `yaac` field rather than those bytes, since that is what the format guarantees; the byte
prefix is for tools inspecting a stream. Everything goes through `dumps`; nothing serializes JSON by itself.

**Participants are addressed by a structure, not a bare string.** An `Address` carries a name and a routing id, and
further locators can be added as fields without changing how anything parses. A bare string would have had to be
reinterpreted to add one.

ZMQ's own vocabulary is kept explicit where it surfaces: what ZMQ calls a routing id is `zmq_routing_id` on the
wire, never a YAAC-flavoured synonym.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any

# ULID -------------------------------------------------------------------
# 128-bit identifier in Crockford base32: a 48-bit millisecond timestamp followed by 80 random bits. Implemented
# here rather than taken as a dependency because it is ~10 lines and the project's dependency set is limited to
# pyzmq and mcp.

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Return a fresh ULID: 48 bits of milliseconds, 80 bits of randomness."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    out = bytearray(26)
    for i in range(25, -1, -1):
        out[i] = ord(_CROCKFORD[value & 0x1F])
        value >>= 5
    return out.decode("ascii")


def utc_now() -> str:
    """Timestamp in the format used by envelopes."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Serialization ----------------------------------------------------------


PROTOCOL_VERSION = 1
"""Stamped onto every message as its first field. Bump when a change would confuse an older reader."""

MAGIC = b'{"yaac":1'
"""What a version 1 message starts with. No trailing comma: a message carrying no other field would end right here,
so the comma is not something the format can promise."""

MAGIC_PREFIX = b'{"yaac":'
"""Version-agnostic prefix, for a reader that wants to recognise a YAAC message before deciding it can read it."""


def dumps(obj: Any) -> bytes:
    """Serialize to JSON bytes with the version stamped first.

    `yaac: PROTOCOL_VERSION` goes ahead of every other field. That is what makes the start of a message bytewise
    stable: whatever the message is, its first nine bytes are `{"yaac":1`, so a reader can recognise a YAAC message
    and the version that wrote it from the prefix alone, without parsing. Stamping it here rather than at each call
    site means no message can be built without it.

    Remaining fields keep the order they were built in, which the constructors below fix, so equal content still
    serializes to equal bytes.

    Separators carry no spaces. Non-ASCII is emitted as UTF-8 rather than `\\u` escapes, which keeps names readable
    and ties the byte sequence to the text rather than to an escaping choice.
    """
    if isinstance(obj, dict):
        obj = {"yaac": PROTOCOL_VERSION, **obj}
    encoded = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # MAGIC is written out independently of PROTOCOL_VERSION, so this catches the two drifting apart -- and any
    # change to separators or stamping that would break the prefix a reader keys on.
    assert encoded.startswith(MAGIC)
    return encoded


def loads(frame: bytes) -> Any:
    """Deserialize a single frame without checking its version. Raises ValueError on malformed input."""
    try:
        return json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed frame: {exc}") from exc


def peek_version(frame: bytes) -> int | None:
    """The protocol version a frame claims, read from its opening bytes without parsing the message.

    For error paths only. A version mismatch is otherwise silent in the worst way: an older hat drops what it
    cannot read and answers nothing, and the newer session waiting on it sees a rendezvous point that accepts
    connections and never replies -- which reads as an empty network. Naming the version turns that into a
    sentence somebody can act on.
    """
    if not frame.startswith(MAGIC_PREFIX):
        return None
    digits = bytearray()
    for byte in frame[len(MAGIC_PREFIX) :]:
        if not chr(byte).isdigit():
            break
        digits.append(byte)
    return int(digits) if digits else None


def parse(frame: bytes) -> Any:
    """Deserialize a received frame and reject anything this build cannot read.

    Checking the parsed `yaac` field rather than the leading bytes is deliberate: it is what the format actually
    guarantees. The byte prefix is a convenience for tools looking at a stream, not the rule -- whitespace or a
    different serializer would defeat it while leaving the message perfectly valid.
    """
    message = loads(frame)
    if not isinstance(message, dict):
        raise ValueError(f"expected an object, got {type(message).__name__}")
    version = message.get("yaac")
    # An exact integer, so 1.0 and True -- both of which equal 1 in Python -- are rejected rather than accepted as
    # version 1 by accident.
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version {version!r}, this build speaks {PROTOCOL_VERSION}")
    return message


# Addresses --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Address:
    """Where a message came from or is going.

    Both locators are optional and identify the same participant by different means:

    * `name` -- the name the user chose. Unique within a channel while its holder is connected, reusable
      afterwards, and meaningful to a human.
    * `routing_id` -- the ULID used as the ZMQ routing id. Unique for the lifetime of one connection and never reused,
      so it stays unambiguous where a name would not.

    A sender fills in whichever it knows; the hat fills in both, from its own table rather than from anything the
    sender claimed.
    """

    name: str | None = None
    routing_id: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """Only the locators this address actually has.

        A locator it does not know is omitted rather than written as null, which is the same rule scopes follow:
        a key that appears carries a value. It is also the shorter wire, and addresses are the most repeated
        structure in the format -- one in `from`, one in `to`, one per member of every roster.

        The routing id is named explicitly: it is a ZMQ transport address, and anything reading this should see
        that rather than guess what "routing_id" refers to.
        """
        wire: dict[str, Any] = {}
        if self.name is not None:
            wire["name"] = self.name
        if self.routing_id is not None:
            wire["zmq_routing_id"] = self.routing_id
        return wire

    @classmethod
    def from_wire(cls, value: Any) -> Address | None:
        """Parse an address field. Returns None for a missing or null one, which means "everybody".

        Nobody is said by leaving the field out, so an address that names nobody -- `{}`, or a locator written as
        null -- is refused rather than read as an address to nowhere.
        """
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"an address must be an object or null, got {type(value).__name__}")
        for field_name in ("name", "zmq_routing_id"):
            if field_name in value and value[field_name] is None:
                raise ValueError(f"address {field_name} is null; omit a locator you do not have")
        name, routing_id = value.get("name"), value.get("zmq_routing_id")
        for field_name, item in (("name", name), ("zmq_routing_id", routing_id)):
            if item is not None and not isinstance(item, str):
                raise ValueError(f"address {field_name} must be a string")
        if name is None and routing_id is None:
            raise ValueError("an address names at least one locator; nobody is said by omitting the field")
        return cls(name=name, routing_id=routing_id)


# Scopes -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Scope:
    """Who a message is for, or who it is from, as fields that compose.

    * `Scope()` -- nothing at all, which addresses whoever wears the hat: technical asks, and the bounces and
      rosters it sends back. The one scope whose mail the hat reads rather than relays.
    * `Scope(channel="forum")` -- everybody on that channel.
    * `Scope(peer=address)` -- that participant, wherever they are.
    * `Scope(channel="forum", peer=address)` -- that participant as a member of that channel.

    **The hat is `{}`, and only `{}`.** `null` and `{"channel": null}` mean the same thing a reader would
    understand, which is exactly why they are refused: one concept deserves one encoding, and a format that
    accepts synonyms has to keep answering which of them is canonical. A key that appears carries a value; there
    is no null inside a scope.

    That leaves no encoding for "everybody on the unnamed channel", and deliberately: an absence pretending to be
    a value is a poor way to say *everyone*. If the world channel is ever built it says so positively, with a
    marker of its own, which no serializer can quietly drop.

    Senders never transmit `from` at all; the hat stamps it from its own table, which is what makes `from: {}`
    unforgeable by construction rather than by validation.
    """

    channel: str | None = None
    peer: Address | None = None

    def to_wire(self) -> dict[str, Any]:
        """Only the fields that carry something. The hat is `{}`, the one spelling this ever writes."""
        wire: dict[str, Any] = {}
        if self.channel is not None:
            wire["channel"] = self.channel
        if self.peer is not None:
            wire["peer"] = self.peer.to_wire()
        return wire

    @classmethod
    def from_wire(cls, value: Any) -> Scope:
        """Parse a scope, refusing every spelling but the one this writes.

        A field that is present carries something: `{"channel": null}` is refused rather than read as the hat,
        because the hat is `{}` and a second way to say it is only a way to disagree later. Unknown fields are
        ignored rather than refused -- the format promises that a later version may add a locator without
        breaking a reader that predates it.
        """
        if not isinstance(value, dict):
            raise ValueError(f"a scope must be an object, got {type(value).__name__}")
        for field_name in ("channel", "peer"):
            if field_name in value and value[field_name] is None:
                raise ValueError(f"scope {field_name} is null; the hat is addressed as {{}} and nothing else")
        channel = value.get("channel")
        if channel is not None and not isinstance(channel, str):
            raise ValueError("scope channel must be a string")
        return cls(channel=channel, peer=Address.from_wire(value.get("peer")))

    @property
    def is_the_hat(self) -> bool:
        """True when nothing is addressed, however that was written."""
        return self.channel is None and self.peer is None


# Data messages ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Destination:
    """Frame 0 of a data message: which channel, and who on it.

    `to=None` broadcasts to the whole channel. The hat validates `channel` against the sender's registered channel
    and otherwise ignores it; it is carried for logging and explicitness, never trusted.
    """

    channel: str
    to: Address | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"channel": self.channel, "to": self.to.to_wire() if self.to else None}

    @classmethod
    def from_wire(cls, value: Any) -> Destination:
        if not isinstance(value, dict):
            raise ValueError("a destination must be an object")
        channel = value.get("channel")
        if channel is not None and not isinstance(channel, str):
            raise ValueError("destination channel must be a string or null")
        return cls(channel=channel, to=Address.from_wire(value.get("to")))


@dataclass(frozen=True, slots=True)
class Envelope:
    """What the hat delivers to a recipient.

    `to` is the recipient's address for a direct message and None for a broadcast. Recipients need the difference to
    choose a reply mode: answering a broadcast privately, or a private message publicly, reaches the wrong people.

    `sender` is filled in by the hat from its routing-id table, so a participant cannot claim to be somebody else.
    """

    id: str
    channel: str
    sender: Address
    to: Address | None
    ts: str
    body: str

    def to_wire(self) -> dict[str, Any]:
        # `dumps` preserves this order, so build the routing header first and leave `body` last: it is the only
        # unbounded field, and `head -c` on a log should show who sent what to whom however long the bodies run.
        return {
            "id": self.id,
            "ts": self.ts,
            "channel": self.channel,
            "from": self.sender.to_wire(),
            "to": self.to.to_wire() if self.to else None,
            "body": self.body,
        }

    @classmethod
    def from_wire(cls, value: dict[str, Any]) -> Envelope:
        sender = Address.from_wire(value.get("from"))
        if sender is None:
            raise ValueError("an envelope must name its sender")
        return cls(
            id=value["id"],
            channel=value["channel"],
            sender=sender,
            to=Address.from_wire(value.get("to")),
            ts=value["ts"],
            body=value["body"],
        )


def envelope(
    *,
    channel: str,
    sender: Address,
    to: Address | None,
    body: str,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Build a delivered envelope, ready to serialize."""
    return Envelope(
        id=msg_id or new_ulid(),
        channel=channel,
        sender=sender,
        to=to,
        ts=utc_now(),
        body=body,
    ).to_wire()


def destination(channel: str, to: Address | None = None) -> dict[str, Any]:
    """Build a destination frame, ready to serialize. `to=None` broadcasts to the channel."""
    return Destination(channel=channel, to=to).to_wire()


def is_control(message: dict[str, Any]) -> bool:
    """True if this is a control message rather than a delivered envelope."""
    return message.get("from") is None and "kind" in message


# Identity ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Identity:
    """What the hat records for one routing id."""

    channel: str
    name: str

    def address(self, routing_id: bytes | str) -> Address:
        """The full address of this participant, both locators filled in."""
        return Address(
            name=self.name,
            routing_id=routing_id.decode("ascii") if isinstance(routing_id, bytes) else routing_id,
        )


# Control messages -------------------------------------------------------
#
# Hat -> participant.


def whois() -> dict[str, Any]:
    """Ask an unregistered routing id to identify itself. Sent by a hat with no entry for it."""
    return {"from": None, "kind": "whois"}


def roster(channel: str, peers: list[Address]) -> dict[str, Any]:
    """Current membership of a channel. Cached by the participant; not written to any inbox."""
    return {
        "from": None,
        "kind": "roster",
        "channel": channel,
        "peers": [p.to_wire() for p in peers],
    }


def bounce(msg_id: str, reason: str) -> dict[str, Any]:
    """Report that a message could not be delivered. Written to the sender's inbox so the failure is readable."""
    return {"from": None, "kind": "bounce", "id": msg_id, "reason": reason}


def error(reason: str) -> dict[str, Any]:
    """Report that a request was refused. Written to the inbox unless it answers an in-flight `hello`."""
    return {"from": None, "kind": "error", "reason": reason}


def channels(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Reply to `channels?`: one entry per occupied channel, each with `name`, `uuid` and member `count`."""
    return {"from": None, "kind": "channels", "channels": entries}


# Participant -> hat.


def hello(channel: str, name: str, reply_to: str) -> dict[str, Any]:
    """Claim a (channel, name) pair for this routing id."""
    return {
        "from": None,
        "kind": "hello",
        "channel": channel,
        "name": name,
        "reply_to": reply_to,
    }


def channels_query() -> dict[str, Any]:
    """Request the channel list. Sent by `Backend.probe_channels`; the sender is not registered by the hat."""
    return {"from": None, "kind": "channels?"}
