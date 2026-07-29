"""The leader: routing table, whois, roster, bounce.

Run by whichever backend succeeded in binding the endpoint. All state in this class is in memory and derivable from
the connected peers: a replacement leader rebuilds it from the ``hello`` messages participants send on reconnect,
so nothing is persisted and no handover protocol is needed.

Routing decisions use only the destination frame and the routing_id table. Message bodies are passed through as opaque
bytes; no method here parses or branches on a body.

Peer liveness without ``ROUTER_NOTIFY``: that option is part of the libzmq draft API and released pyzmq wheels bundle
libzmq built without draft support, so ``setsockopt`` rejects it with ``EINVAL``. Two standard mechanisms replace it:

* arrival -- each participant re-sends ``hello`` when its DEALER monitor reports ``EVENT_CONNECTED``, which also fires
  after a leader changeover, so the new leader is told who is present without asking;
* departure -- ``ROUTER_MANDATORY`` makes ``send_multipart`` raise ``EHOSTUNREACH`` for a routing_id that has gone away,
  which is where ``evict`` is called from.

``whois`` covers the remaining case: a data message arriving from a routing_id absent from the table.
"""

import time
from dataclasses import dataclass, field
from typing import Any

import zmq

from . import protocol
from .protocol import Address, Identity

# Bounds on `pending`, so a routing_id that never answers `whois` cannot grow the dict without limit. On overflow the
# sender receives a bounce instead of the message being held indefinitely.
PENDING_MAX_PER_PARTICIPANT = 8
PENDING_MAX_AGE_SECONDS = 5.0


@dataclass
class ChannelInfo:
    """One channel's membership. `uuid` is reported by `list_channels` and is not used for routing."""

    name: str
    uuid: str = field(default_factory=protocol.new_ulid)
    members: set[bytes] = field(default_factory=set)


@dataclass
class Held:
    """A data message held while a `whois` for its sender is outstanding. `at` is a `time.monotonic()` reading."""

    dest_frame: bytes
    body: bytes
    at: float


class Leader:
    """Routing table and message switch. Owns the ROUTER socket but runs no loop; `Backend._pump_router` calls in."""

    def __init__(self, router: zmq.Socket, log: Any) -> None:
        self.router = router
        self.log = log
        self.routing_ids: dict[bytes, Identity] = {}
        self.by_name: dict[tuple[str, str], bytes] = {}
        self.channels: dict[str, ChannelInfo] = {}
        self.pending: dict[bytes, list[Held]] = {}
        self.whois_inflight: set[bytes] = set()

    # -- transmission ----------------------------------------------------

    def _send(self, routing_id: bytes, message: dict[str, Any]) -> bool:
        """Send one message to a routing_id.

        Returns False and evicts the routing_id when the ROUTER reports the peer unreachable. This is the only place a
        departure is detected, since `ROUTER_NOTIFY` disconnect events are unavailable.
        """
        try:
            self.router.send_multipart([routing_id, protocol.dumps(message)], zmq.NOBLOCK)
            return True
        except zmq.ZMQError as exc:
            if exc.errno in (zmq.EHOSTUNREACH, zmq.EAGAIN):
                self.log(f"peer unreachable, evicting {routing_id!r}: {zmq.strerror(exc.errno)}")
                self.evict(routing_id)
                return False
            raise

    def _reachable(self, routing_id: bytes) -> bool:
        """Test whether a routing_id is still connected, by sending it a `whois`.

        A connected participant replies with `hello`, which `_hello` treats as idempotent. A departed one raises
        `EHOSTUNREACH` inside `_send`, which evicts it and returns False.
        """
        return self._send(routing_id, protocol.whois())

    # -- membership ------------------------------------------------------

    def evict(self, routing_id: bytes) -> None:
        """Remove a routing_id from every table and send the remaining members an updated roster."""
        self.pending.pop(routing_id, None)
        self.whois_inflight.discard(routing_id)
        if (identity := self.routing_ids.pop(routing_id, None)) is None:
            return
        self.by_name.pop((identity.channel, identity.name), None)
        if (channel := self.channels.get(identity.channel)) is not None:
            channel.members.discard(routing_id)
            if channel.members:
                self.broadcast_roster(identity.channel)
            else:
                # Channels are not persistent objects: drop it once the last member leaves, so `list_channels`
                # reports only occupied ones and `connect` can report `created` correctly.
                del self.channels[identity.channel]

    def members(self, channel_name: str) -> set[bytes]:
        return channel.members.copy() if (channel := self.channels.get(channel_name)) else set()

    def names(self, channel_name: str) -> list[str]:
        return sorted(self.routing_ids[h].name for h in self.members(channel_name) if h in self.routing_ids)

    def addresses(self, channel_name: str) -> list[Address]:
        """Every member of a channel, with both locators filled in from this table."""
        return sorted(
            (self.routing_ids[h].address(h) for h in self.members(channel_name) if h in self.routing_ids),
            key=lambda a: (a.name or "", a.routing_id or ""),
        )

    def broadcast_roster(self, channel_name: str) -> None:
        """Send every member of a channel the current name list. Participants cache it; it is not written to
        inboxes."""
        message = protocol.roster(channel_name, self.addresses(channel_name))
        for routing_id in self.members(channel_name):
            self._send(routing_id, message)

    def channel_report(self) -> list[dict[str, Any]]:
        """Name, uuid and member count of every occupied channel, as answered to a `channels?` query."""
        return [{"name": c.name, "uuid": c.uuid, "count": len(c.members)} for c in self.channels.values()]

    # -- inbound ---------------------------------------------------------

    def handle_frames(self, frames: list[bytes]) -> None:
        """Dispatch one multipart message received on the ROUTER socket.

        Frame 0 is the sender's routing id, set by libzmq from the connection rather than from message content, so it
        identifies the sender reliably. A single remaining frame is a control message; two are a data message's
        destination and body.
        """
        source, *rest = frames
        match rest:
            case [single]:
                try:
                    message = protocol.parse(single)
                except ValueError as exc:
                    self.log(f"dropping unreadable control frame from {source!r}: {exc}")
                    return
                self._control(source, message)
            case [dest_frame, body]:
                self._data(source, dest_frame, body)
            case _:
                self.log(f"dropping {len(rest)}-frame message from {source!r}")

    def _control(self, source: bytes, message: dict[str, Any]) -> None:
        match message.get("kind"):
            case "hello":
                self._hello(source, message)
            case "channels?":
                # Answer without adding the sender to any table. `list_channels` uses a throwaway DEALER, and
                # registering it would put a non-participant into rosters and member counts.
                self._send(source, protocol.channels(self.channel_report()))
            case unknown:
                self.log(f"ignoring control message {unknown!r} from {source!r}")

    def _hello(self, source: bytes, message: dict[str, Any]) -> None:
        """Bind a (channel, name) pair to a routing_id.

        Participants send this on every DEALER connect, so it must be idempotent: after a leader changeover all of them
        re-announce at once and most are restating an identity this table already holds.
        """
        channel_name = message.get("channel")
        name = message.get("name")
        if not isinstance(channel_name, str) or not isinstance(name, str):
            self._send(source, protocol.error("hello needs a channel and a name"))
            return

        key = (channel_name, name)
        if (incumbent := self.by_name.get(key)) is not None and incumbent != source:
            # The name is bound to a different routing_id. Refuse rather than reassign, because reassigning would
            # deliver messages intended for the incumbent to the newcomer. The exception is an incumbent that no
            # longer has a live connection -- typically a session killed while its TCP connection was still open --
            # which would otherwise block the user from reusing their own name.
            if self._reachable(incumbent):
                self._send(source, protocol.error("name taken on this channel"))
                return
            self.log(f"evicted unreachable incumbent for {key!r}")

        # A routing_id re-announcing under a different name gives up the old one.
        if (previous := self.routing_ids.get(source)) is not None and previous != Identity(*key):
            self.by_name.pop((previous.channel, previous.name), None)
            if (old := self.channels.get(previous.channel)) is not None:
                old.members.discard(source)

        identity = Identity(channel=channel_name, name=name)
        changed = self.routing_ids.get(source) != identity

        channel = self.channels.setdefault(channel_name, ChannelInfo(name=channel_name))
        channel.members.add(source)
        self.routing_ids[source] = identity
        self.by_name[key] = source
        self.whois_inflight.discard(source)
        # Log only identity changes: reconnect-driven hellos would otherwise repeat a line per participant on every
        # changeover.
        if changed:
            self.log(f"hello: {name!r} on {channel_name!r} as {source!r}")
        self._flush_pending(source)
        self.broadcast_roster(channel_name)

    def _data(self, source: bytes, dest_frame: bytes, body: bytes) -> None:
        """Route one data message. The sender's channel is read from `routing_ids`, not from the destination frame."""
        if (identity := self.routing_ids.get(source)) is None:
            self._hold(source, dest_frame, body)
            return

        try:
            destination = protocol.Destination.from_wire(protocol.parse(dest_frame))
        except ValueError as exc:
            self.log(f"dropping unreadable destination from {source!r}: {exc}")
            return

        # `channel` in the destination frame is checked against the sender's registered channel and otherwise
        # unused. Because targets are always resolved within `identity.channel`, a participant cannot address a channel
        # it
        # has not joined even if it names one here.
        if destination.channel is not None and destination.channel != identity.channel:
            self._send(source, protocol.error(f"you are not on channel {destination.channel!r}"))
            return

        message_id = protocol.new_ulid()
        recipient = destination.to
        if recipient is None:
            targets = self.members(identity.channel) - {source}
            to_address = None
        else:
            # A recipient may be named by either locator. The routing_id is checked first: it identifies one connection
            # and is never reused, whereas a name is only unique while its holder is connected.
            target = None
            if recipient.routing_id is not None:
                candidate = recipient.routing_id.encode("ascii")
                if candidate in self.routing_ids and self.routing_ids[candidate].channel == identity.channel:
                    target = candidate
            if target is None and recipient.name is not None:
                target = self.by_name.get((identity.channel, recipient.name))
            if target is None:
                self._send(source, protocol.bounce(message_id, "no such recipient on this channel"))
                return
            targets = {target}
            to_address = self.routing_ids[target].address(target)

        envelope = protocol.envelope(
            channel=identity.channel,
            sender=identity.address(source),
            to=to_address,
            body=body.decode("utf-8", errors="replace"),
            msg_id=message_id,
        )
        for routing_id in targets:
            if not self._send(routing_id, envelope) and recipient is not None:
                # The recipient's routing_id became unreachable between target lookup and send. Only direct messages
                # bounce: a broadcast that fails for one member still reached the others.
                self._send(source, protocol.bounce(message_id, "recipient went away"))

    # -- whois -----------------------------------------------------------

    def _hold(self, source: bytes, dest_frame: bytes, body: bytes) -> None:
        """Hold a message from an unregistered routing_id and send that routing_id a `whois`.

        Holding rather than bouncing means a send issued during a leader changeover is delivered late instead of
        failing, so the caller sees no error. `_flush_pending` replays these once `hello` arrives.
        """
        held = self.pending.setdefault(source, [])
        now = time.monotonic()
        held[:] = [h for h in held if now - h.at < PENDING_MAX_AGE_SECONDS]

        if len(held) >= PENDING_MAX_PER_PARTICIPANT:
            self._send(source, protocol.bounce(protocol.new_ulid(), "sender not identified"))
            return

        held.append(Held(dest_frame=dest_frame, body=body, at=now))
        if source not in self.whois_inflight:
            self.whois_inflight.add(source)
            self._send(source, protocol.whois())

    def _flush_pending(self, source: bytes) -> None:
        now = time.monotonic()
        for item in self.pending.pop(source, []):
            if now - item.at < PENDING_MAX_AGE_SECONDS:
                self._data(source, item.dest_frame, item.body)
