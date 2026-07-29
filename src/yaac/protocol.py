"""Wire protocol: envelopes, control messages, serialization.

Everything on the wire is JSON. Two kinds of message travel between a spoke and
the hub:

* **data** -- a spoke sends ``[dest_json][body]``; the hub delivers
  ``[envelope_json]``.
* **control** -- a single JSON frame with a ``kind`` field.

Control messages are distinguished from envelopes by carrying ``"from": null``
rather than by any reserved nickname or channel name. Nicknames and channel
names are raw UTF-8 chosen by the user; the protocol reserves none of them.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any

# ULID -------------------------------------------------------------------
# 128-bit identifier in Crockford base32: a 48-bit millisecond timestamp followed
# by 80 random bits. Implemented here rather than taken as a dependency because it
# is ~10 lines and the project's dependency set is limited to pyzmq and mcp.

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


def dumps(obj: Any) -> bytes:
    """Serialize a protocol object to a single frame."""
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def loads(frame: bytes) -> Any:
    """Deserialize a single frame. Raises ValueError on malformed input."""
    try:
        return json.loads(frame.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"malformed frame: {exc}") from exc


# Identity ---------------------------------------------------------------


@dataclass(frozen=True)
class Identity:
    """Who a handle belongs to, as far as the hub is concerned."""

    channel: str
    nickname: str


# Destination (spoke -> hub, frame 0 of a data message) -------------------


def destination(channel: str, nickname: str | None = None) -> dict[str, Any]:
    """Address a message. ``nickname=None`` broadcasts to the whole channel."""
    return {"channel": channel, "nickname": nickname}


# Envelope (hub -> spoke) ------------------------------------------------


def envelope(
    *,
    channel: str,
    sender: str,
    to: str | None,
    body: Any,
    msg_id: str | None = None,
) -> dict[str, Any]:
    """Build the envelope the hub delivers to a recipient.

    ``to`` is the recipient's nickname for a direct message and None for a broadcast. Recipients need this to choose
    a reply mode: replying to ``to=None`` with a direct message, or to a direct message with a broadcast, sends the
    reply to the wrong set of participants.

    ``sender`` must be supplied by the hub from its handle table, not from any field the sending spoke provided.
    """
    return {
        "id": msg_id or new_ulid(),
        "channel": channel,
        "from": sender,
        "to": to,
        "ts": utc_now(),
        "body": body,
    }


def is_control(message: dict[str, Any]) -> bool:
    """True if this is a control message rather than a delivered envelope."""
    return message.get("from") is None and "kind" in message


# Control messages -------------------------------------------------------
#
# Hub -> spoke.


def whois() -> dict[str, Any]:
    """Ask an unknown handle to identify itself. Sent by a hub with no table."""
    return {"from": None, "kind": "whois"}


def roster(channel: str, peers: list[str]) -> dict[str, Any]:
    """Current membership of a channel. Updates the spoke's cache; not inboxed."""
    return {"from": None, "kind": "roster", "channel": channel, "peers": peers}


def bounce(msg_id: str, reason: str) -> dict[str, Any]:
    """Report that a message could not be delivered. Written to the sender's inbox so the failure is readable."""
    return {"from": None, "kind": "bounce", "id": msg_id, "reason": reason}


def error(reason: str) -> dict[str, Any]:
    """Report that a request was refused. Written to the inbox unless it answers an in-flight ``hello``."""
    return {"from": None, "kind": "error", "reason": reason}


def channels(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Reply to `channels?`: one entry per occupied channel, each with `name`, `uuid` and member `count`."""
    return {"from": None, "kind": "channels", "channels": entries}


# Spoke -> hub.


def hello(channel: str, nickname: str, reply_to: str) -> dict[str, Any]:
    """Claim a (channel, nickname) for this handle."""
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
