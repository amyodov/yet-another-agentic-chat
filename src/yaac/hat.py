"""The hat: routing table, whois, roster, bounce.

Worn by whichever backend won the bind. The hat confers the job of passing everyone else's messages along and nothing
else -- no policy, no configuration, no say over what participants may do. It is put on by getting there first and
comes off when the process exits; the next backend to win the bind picks it up.

All state here is in memory and derivable from the connected peers, so a fresh hat rebuilds it from the ``hello``
messages participants send on reconnect. Nothing is persisted and there is no handover protocol.

Routing uses only the destination frame and the routing-id table. Bodies pass through as opaque bytes; no method here
parses or branches on one.

Peer liveness without ``ROUTER_NOTIFY``: that option is part of the libzmq draft API and released pyzmq wheels bundle
libzmq built without draft support, so ``setsockopt`` rejects it with ``EINVAL``. Two standard mechanisms replace it:

* arrival -- each participant re-sends ``hello`` when its DEALER monitor reports ``EVENT_CONNECTED``, which also fires
  after the hat changes heads, so a new one learns who is present without asking;
* departure -- ``ROUTER_MANDATORY`` makes ``send_multipart`` raise ``EHOSTUNREACH`` for a routing id that has gone
  away, which is where ``evict`` is called from.

``whois`` covers the remaining case: a data message from a routing id absent from the table.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import zmq

from . import protocol
from .protocol import Address, Identity, Scope

logger = logging.getLogger(__name__)

# Bounds on `pending`, so a routing id that never answers `whois` cannot grow the dict without limit. On overflow the
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

    envelope: protocol.Envelope
    body: bytes
    at: float


class Hat:
    """Routing table and message switch. Owns the ROUTER socket but runs no loop; `Backend._pump_router` calls in."""

    def __init__(self, router: zmq.Socket) -> None:
        self.router = router
        self.routing_ids: dict[bytes, Identity] = {}
        self.by_name: dict[tuple[str, str], bytes] = {}
        self.channels: dict[str, ChannelInfo] = {}
        self.pending: dict[bytes, list[Held]] = {}
        # Which participant is behind a connection, as told by `hello`. Soft state like everything else here, and
        # rebuilt after a changeover by the hellos that follow it.
        self.peer_uids: dict[bytes, str] = {}
        self.whois_inflight: set[bytes] = set()

    # -- transmission ----------------------------------------------------

    def _send(self, routing_id: bytes, message: protocol.Envelope) -> bool:
        """Send one message to a routing id.

        Returns False and evicts it when the ROUTER reports the peer unreachable. This is the only place a
        departure is detected, since `ROUTER_NOTIFY` disconnect events are unavailable.
        """
        try:
            self.router.send_multipart([routing_id, protocol.dumps(message.to_wire())], zmq.NOBLOCK)
            return True
        except zmq.ZMQError as exc:
            if exc.errno in (zmq.EHOSTUNREACH, zmq.EAGAIN):
                logger.info("peer unreachable, evicting %r: %s", routing_id, zmq.strerror(exc.errno))
                self.evict(routing_id)
                return False
            raise

    def _reachable(self, routing_id: bytes) -> bool:
        """Test whether a routing id is still connected, by sending it a `whois`.

        A connected participant replies with `hello`, which `_hello` treats as idempotent. A departed one raises
        `EHOSTUNREACH` inside `_send`, which evicts it and returns False.
        """
        return self._send(routing_id, protocol.whois(to=Scope(peer=Address(routing_id=routing_id.decode()))))

    # -- membership ------------------------------------------------------

    def evict(self, routing_id: bytes) -> None:
        """Remove a routing id from every table and send the remaining members an updated roster."""
        self.pending.pop(routing_id, None)
        self.whois_inflight.discard(routing_id)
        self.peer_uids.pop(routing_id, None)
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
        members = self.addresses(channel_name)
        for routing_id in self.members(channel_name):
            self._send(routing_id, protocol.roster(channel_name, members, to=self._scope_of(routing_id)))

    def channel_report(self) -> list[dict[str, Any]]:
        """Name, uuid and member count of every occupied channel, as answered to a `channels?` query."""
        return [{"name": c.name, "uuid": c.uuid, "count": len(c.members)} for c in self.channels.values()]

    # -- inbound ---------------------------------------------------------

    def handle_frames(self, frames: list[bytes]) -> None:
        """Dispatch one message received on the ROUTER socket.

        Frame 0 is the sender's routing id, set by libzmq from the connection rather than from message content, so
        it identifies the sender reliably. Frame 1 is the whole message, whatever it is: what decides between
        obeying and carrying is who it is addressed to, not how many frames arrived.
        """
        source, *rest = frames
        if len(rest) != 1:
            logger.warning("dropping %d-frame message from %r", len(rest), source)
            return
        try:
            envelope = protocol.Envelope.from_wire(protocol.parse(rest[0]))
        except ValueError as exc:
            # Naming the version turns "an endpoint that answers nothing" into a sentence somebody can act on.
            seen = protocol.peek_version(rest[0])
            logger.warning("dropping unreadable message from %r (protocol %s): %s", source, seen, exc)
            return
        if envelope.for_the_hat:
            self._operator(source, envelope)
        else:
            self._relay(source, envelope)

    def _scope_of(self, routing_id: bytes) -> Scope:
        """How to address one routing id, with whatever this table knows about it."""
        if (identity := self.routing_ids.get(routing_id)) is not None:
            return Scope(channel=identity.channel, peer=identity.address(routing_id))
        return Scope(peer=Address(routing_id=routing_id.decode()))

    def _operator(self, source: bytes, envelope: protocol.Envelope) -> None:
        """The only mail the hat reads. Hard rule 5, restated: obedience is decided by addressing."""
        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        match envelope.op:
            case "hello":
                self._hello(source, payload)
            case "channels":
                # Answered without adding the sender to any table. `list_channels` uses a throwaway DEALER, and
                # registering it would put a non-participant into rosters and member counts.
                self._send(source, protocol.channels(self.channel_report(), to=self._scope_of(source)))
            case unknown:
                logger.warning("ignoring operator message %r from %r", unknown, source)

    def _hello(self, source: bytes, payload: dict[str, Any]) -> None:
        """Bind a (channel, name) pair to a routing_id.

        Participants send this on every DEALER connect, so it must be idempotent: after a hat changeover all of them
        re-announce at once and most are restating an identity this table already holds.
        """
        channel_name = payload.get("channel")
        name = payload.get("name")
        if not isinstance(channel_name, str) or not isinstance(name, str):
            self._send(source, protocol.error("hello needs a channel and a name", to=self._scope_of(source)))
            return

        peer_uid = payload.get("peer_uid") if isinstance(payload.get("peer_uid"), str) else None
        key = (channel_name, name)
        incumbent = self.by_name.get(key)
        returning = (
            incumbent is not None
            and incumbent != source
            and peer_uid is not None
            and self.peer_uids.get(incumbent) == peer_uid
        )
        if returning:
                # The same participant coming back on a new connection -- a client restarted, or a DEALER
                # reconnected under a new routing id. Without this the name is held by a connection nobody is
                # behind until a send to it happens to fail, and its owner is locked out of their own name.
                # The uid is not a secret and proves nothing: it prevents the accident, not a determined session.
                logger.info("peer %r returning on a new connection for %r", peer_uid, key)
                self.evict(incumbent)
                incumbent = None
        if incumbent is not None and incumbent != source:
            # The name is bound to a different routing_id. Refuse rather than reassign, because reassigning would
            # deliver messages intended for the incumbent to the newcomer. The exception is an incumbent that no
            # longer has a live connection -- typically a session killed while its TCP connection was still open --
            # which would otherwise block the user from reusing their own name.
            if self._reachable(incumbent):
                self._send(source, protocol.error("name taken on this channel", to=self._scope_of(source)))
                return
            logger.info("evicted unreachable incumbent for %r", key)

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
        if peer_uid is not None:
            self.peer_uids[source] = peer_uid
        self.whois_inflight.discard(source)
        # Log only identity changes: reconnect-driven hellos would otherwise repeat a line per participant on every
        # changeover.
        if changed:
            logger.info("hello: %r on %r as %r", name, channel_name, source)
        self._flush_pending(source)
        self.broadcast_roster(channel_name)

    def _relay(self, source: bytes, envelope: protocol.Envelope) -> None:
        """Carry one message to whoever its scope names. The sender's channel comes from `routing_ids`, never from
        the message: that is what makes addressing a channel you have not joined impossible rather than forbidden.
        """
        if (identity := self.routing_ids.get(source)) is None:
            self._hold(source, envelope)
            return

        scope = envelope.to
        # A named channel is checked against the sender's own and otherwise unused. Targets are always resolved
        # inside `identity.channel`, so naming another one here changes nothing -- it is refused for clarity.
        if scope.channel is not None and scope.channel != identity.channel:
            self._send(source, protocol.error(f"you are not on channel {scope.channel!r}", to=self._scope_of(source)))
            return

        recipient = scope.peer
        if recipient is None:
            targets = self.members(identity.channel) - {source}
            to_scope = Scope(channel=identity.channel)
        else:
            # Either locator names a recipient. The routing id is tried first: it identifies one connection and is
            # never reused, where a name is unique only while its holder is connected.
            target = None
            if recipient.routing_id is not None:
                candidate = recipient.routing_id.encode("ascii")
                if candidate in self.routing_ids and self.routing_ids[candidate].channel == identity.channel:
                    target = candidate
            if target is None and recipient.name is not None:
                target = self.by_name.get((identity.channel, recipient.name))
            if target is None:
                self._send(
                    source,
                    protocol.bounce(envelope.id, "no such recipient on this channel", to=self._scope_of(source)),
                )
                return
            targets = {target}
            to_scope = Scope(channel=identity.channel, peer=self.routing_ids[target].address(target))

        carried = protocol.message(
            to_scope,
            frm=Scope(channel=identity.channel, peer=identity.address(source)),
            body=envelope.body,
            payload=envelope.payload,
            mentions=self._complete(identity.channel, envelope.mentions),
            tags=envelope.tags,
            msg_id=envelope.id,
        )
        for routing_id in targets:
            if not self._send(routing_id, carried) and recipient is not None:
                # The recipient became unreachable between lookup and send. Only a directed message bounces: a
                # broadcast that failed for one member still reached the others.
                self._send(source, protocol.bounce(envelope.id, "recipient went away", to=self._scope_of(source)))

    def _complete(self, channel_name: str, mentions: tuple[Address, ...]) -> tuple[Address, ...]:
        """Fill in what this table knows about each mentioned participant.

        A mention is social, not delivery: it says who is called on to react, while everyone in scope still hears
        it. Completing the address lets a recipient answer "am I mentioned?" by comparing routing ids, which is
        exact where a name is ambiguous the moment it has been reused. A mention naming somebody absent is carried
        as written -- a bounce is about delivery, and *"Bob, if you are here"* is a normal thing to say.
        """
        completed = []
        for mention in mentions:
            target = None
            if mention.routing_id is not None:
                target = mention.routing_id.encode("ascii")
            elif mention.name is not None:
                target = self.by_name.get((channel_name, mention.name))
            if target is not None and (identity := self.routing_ids.get(target)) is not None:
                completed.append(identity.address(target))
            else:
                completed.append(mention)
        return tuple(completed)

    # -- whois -----------------------------------------------------------

    def _hold(self, source: bytes, envelope: protocol.Envelope) -> None:
        """Hold a message from an unregistered routing id and ask that routing id who it is.

        Holding rather than bouncing means a send issued during a hat changeover arrives late instead of failing,
        so the caller sees no error. `_flush_pending` replays these once `hello` arrives.
        """
        held = self.pending.setdefault(source, [])
        now = time.monotonic()
        held[:] = [h for h in held if now - h.at < PENDING_MAX_AGE_SECONDS]

        if len(held) >= PENDING_MAX_PER_PARTICIPANT:
            self._send(source, protocol.bounce(envelope.id, "sender not identified", to=self._scope_of(source)))
            return

        held.append(Held(envelope=envelope, at=now))
        if source not in self.whois_inflight:
            self.whois_inflight.add(source)
            self._send(source, protocol.whois(to=self._scope_of(source)))

    def _flush_pending(self, source: bytes) -> None:
        now = time.monotonic()
        for item in self.pending.pop(source, []):
            if now - item.at < PENDING_MAX_AGE_SECONDS:
                self._relay(source, item.envelope)
