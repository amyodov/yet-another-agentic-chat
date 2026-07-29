"""Wire protocol: addresses, envelopes, control messages, and canonical serialization.

Everything on the wire is JSON. Two kinds of message travel between a spoke and the hub:

* **data** -- a spoke sends `[destination JSON][body]`; the hub delivers `[envelope JSON]`.
* **control** -- a single JSON frame with a `kind` field.

Control messages are told apart from envelopes by carrying `"from": null` rather than by a reserved nickname or
channel name. Users may choose any string as a nickname, so the protocol reserves none.

Two properties are deliberate and depended on elsewhere:

**Serialization is canonical, with a fixed field order.** `dumps` writes fields in the order given by
`FIELD_ORDER`, not alphabetically, and stamps every message with the protocol version first. Every YAAC message
therefore begins with the same literal bytes:

    {"yaac":1,

which is a magic number: a reader can recognise a YAAC message, and tell which protocol version wrote it, from the
first ten bytes, without parsing. The rest of the header -- `kind`, `id`, `ts`, `channel`, `from`, `to` -- follows in
a fixed order too, so `head -c 200` on a log shows the routing of every message even when the bodies are long.
`body` is always last for that reason.

Field order being fixed also makes the encoding byte-stable: equal content produces equal bytes regardless of the
order fields were built in, so a message has a stable identity to hash or sign. Every frame and every inbox line
goes through `dumps`; nothing serializes JSON by itself.

**Participants are addressed by a structure, not a bare string.** An `Address` currently carries a nickname and a
handle, and further locators can be added as fields without changing how anything is parsed. A bare string would have
had to be reinterpreted to add one.
"""

import json
import os
import time
from dataclasses import asdict, dataclass
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

MAGIC = b'{"yaac":1,'
"""The bytes every serialized message starts with. Recognises a YAAC message and its version without parsing."""

FIELD_ORDER = (
    "yaac",  # magic and version, always first
    "kind",  # control messages: what this is
    "id",
    "ts",
    "channel",
    "from",
    "to",
    "nickname",  # inside an address
    "handle",
    "reply_to",
    "reason",
    "peers",
    "channels",
    "name",
    "uuid",
    "count",
)
"""Fixed serialization order. Fields not named here are written after these, alphabetically, and `body` last of
all -- it is the only unbounded field, so keeping it at the end leaves the whole header near the start of the line."""

_FIELD_RANK = {name: index for index, name in enumerate(FIELD_ORDER)}


def _rank(item: tuple[str, Any]) -> tuple[int, int, str]:
    key = item[0]
    if key == "body":
        return (2, 0, "")
    if (known := _FIELD_RANK.get(key)) is not None:
        return (0, known, "")
    return (1, 0, key)


def _ordered(value: Any) -> Any:
    """Recursively rebuild dicts in FIELD_ORDER. Python preserves insertion order, so json writes them this way."""
    if isinstance(value, dict):
        return {key: _ordered(item) for key, item in sorted(value.items(), key=_rank)}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def dumps(obj: Any) -> bytes:
    """Serialize to canonical JSON bytes, version-stamped and in fixed field order.

    A top-level dict is stamped with `yaac: PROTOCOL_VERSION`, so the magic number cannot be forgotten at any call
    site. Fields are written in `FIELD_ORDER` rather than alphabetically, and separators carry no spaces, so equal
    content serializes to equal bytes on any machine and in any Python version.

    Non-ASCII is emitted as UTF-8 rather than `\\u` escapes, which keeps names readable in the inbox files and ties
    the byte sequence to the text rather than to an escaping choice.
    """
    if isinstance(obj, dict):
        obj = {"yaac": PROTOCOL_VERSION, **obj}
    return json.dumps(_ordered(obj), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(frame: bytes) -> Any:
    """Deserialize a single frame. Raises ValueError on malformed input."""
    try:
        return json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed frame: {exc}") from exc


# Addresses --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Address:
    """Where a message came from or is going.

    Both locators are optional and identify the same participant by different means:

    * `nickname` -- the name the user chose. Unique within a channel while its holder is connected, reusable
      afterwards, and meaningful to a human.
    * `handle` -- the ULID used as the ZMQ routing id. Unique for the lifetime of one connection and never reused,
      so it stays unambiguous where a nickname would not.

    A sender fills in whichever it knows; the hub fills in both, from its own table rather than from anything the
    sender claimed.
    """

    nickname: str | None = None
    handle: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, value: Any) -> Address | None:
        """Parse an address field. Returns None for a missing or null one, which means "everybody"."""
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"an address must be an object or null, got {type(value).__name__}")
        nickname, handle = value.get("nickname"), value.get("handle")
        for name, item in (("nickname", nickname), ("handle", handle)):
            if item is not None and not isinstance(item, str):
                raise ValueError(f"address {name} must be a string or null")
        return cls(nickname=nickname, handle=handle)


# Data messages ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Destination:
    """Frame 0 of a data message: which channel, and who on it.

    `to=None` broadcasts to the whole channel. The hub validates `channel` against the sender's registered channel
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
    """What the hub delivers to a recipient.

    `to` is the recipient's address for a direct message and None for a broadcast. Recipients need the difference to
    choose a reply mode: answering a broadcast privately, or a private message publicly, reaches the wrong people.

    `sender` is filled in by the hub from its handle table, so a participant cannot claim to be somebody else.
    """

    id: str
    channel: str
    sender: Address
    to: Address | None
    ts: str
    body: str

    def to_wire(self) -> dict[str, Any]:
        return {
            "body": self.body,
            "channel": self.channel,
            "from": self.sender.to_wire(),
            "id": self.id,
            "to": self.to.to_wire() if self.to else None,
            "ts": self.ts,
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
    """What the hub records for one handle."""

    channel: str
    nickname: str

    def address(self, handle: bytes | str) -> Address:
        """The full address of this participant, both locators filled in."""
        return Address(
            nickname=self.nickname,
            handle=handle.decode("ascii") if isinstance(handle, bytes) else handle,
        )


# Control messages -------------------------------------------------------
#
# Hub -> spoke.


def whois() -> dict[str, Any]:
    """Ask an unregistered handle to identify itself. Sent by a hub with no entry for it."""
    return {"from": None, "kind": "whois"}


def roster(channel: str, peers: list[Address]) -> dict[str, Any]:
    """Current membership of a channel. Cached by the spoke; not written to any inbox."""
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


# Spoke -> hub.


def hello(channel: str, nickname: str, reply_to: str) -> dict[str, Any]:
    """Claim a (channel, nickname) pair for this handle."""
    return {
        "from": None,
        "kind": "hello",
        "channel": channel,
        "nickname": nickname,
        "reply_to": reply_to,
    }


def channels_query() -> dict[str, Any]:
    """Request the channel list. Sent by `Backend.probe_channels`; the sender is not registered by the hub."""
    return {"from": None, "kind": "channels?"}
