"""The hub: routing table, whois, roster, bounce.

Run by whichever backend succeeded in binding the endpoint. All state in this class is in memory and derivable from
the connected peers: a replacement hub rebuilds it from the ``hello`` messages spokes send on reconnect, so nothing
is persisted and no handover protocol is needed.

Routing decisions use only the destination frame and the handle table. Message bodies are passed through as opaque
bytes; no method here parses or branches on a body.

Peer liveness without ``ROUTER_NOTIFY``: that option is part of the libzmq draft API and released pyzmq wheels bundle
libzmq built without draft support, so ``setsockopt`` rejects it with ``EINVAL``. Two standard mechanisms replace it:

* arrival -- each spoke re-sends ``hello`` when its DEALER monitor reports ``EVENT_CONNECTED``, which also fires
  after a hub changeover, so the new hub is told who is present without asking;
* departure -- ``ROUTER_MANDATORY`` makes ``send_multipart`` raise ``EHOSTUNREACH`` for a handle that has gone away,
  which is where ``evict`` is called from.

``whois`` covers the remaining case: a data message arriving from a handle absent from the table.
"""

import time
from dataclasses import dataclass, field
from typing import Any

import zmq

from . import protocol
from .protocol import Identity

# Bounds on `pending`, so a handle that never answers `whois` cannot grow the dict without limit. On overflow the
# sender receives a bounce instead of the message being held indefinitely.
PENDING_MAX_PER_HANDLE = 8
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


class Hub:
    """Routing table and message switch. Owns the ROUTER socket but runs no loop; `Backend._pump_router` calls in."""

    def __init__(self, router: zmq.Socket, log: Any) -> None:
        self.router = router
        self.log = log
        self.handles: dict[bytes, Identity] = {}
        self.by_name: dict[tuple[str, str], bytes] = {}
        self.channels: dict[str, ChannelInfo] = {}
        self.pending: dict[bytes, list[Held]] = {}
        self.whois_inflight: set[bytes] = set()

    # -- transmission ----------------------------------------------------

    def _send(self, handle: bytes, message: dict[str, Any]) -> bool:
        """Send one message to a handle.

        Returns False and evicts the handle when the ROUTER reports the peer unreachable. This is the only place a
        departure is detected, since `ROUTER_NOTIFY` disconnect events are unavailable.
        """
        try:
            self.router.send_multipart([handle, protocol.dumps(message)], zmq.NOBLOCK)
            return True
        except zmq.ZMQError as exc:
            if exc.errno in (zmq.EHOSTUNREACH, zmq.EAGAIN):
                self.log(f"peer unreachable, evicting {handle!r}: {zmq.strerror(exc.errno)}")
                self.evict(handle)
                return False
            raise

    def _reachable(self, handle: bytes) -> bool:
        """Test whether a handle is still connected, by sending it a `whois`.

        A connected spoke replies with `hello`, which `_hello` treats as idempotent. A departed one raises
        `EHOSTUNREACH` inside `_send`, which evicts it and returns False.
        """
        return self._send(handle, protocol.whois())

    # -- membership ------------------------------------------------------

    def evict(self, handle: bytes) -> None:
        """Remove a handle from every table and send the remaining members an updated roster."""
        self.pending.pop(handle, None)
        self.whois_inflight.discard(handle)
        if (identity := self.handles.pop(handle, None)) is None:
            return
        self.by_name.pop((identity.channel, identity.nickname), None)
        if (channel := self.channels.get(identity.channel)) is not None:
            channel.members.discard(handle)
            if channel.members:
                self.broadcast_roster(identity.channel)
            else:
                # Channels are not persistent objects: drop it once the last member leaves, so `list_channels`
                # reports only occupied ones and `connect` can report `created` correctly.
                del self.channels[identity.channel]

    def members(self, channel_name: str) -> set[bytes]:
        return channel.members.copy() if (channel := self.channels.get(channel_name)) else set()

    def nicknames(self, channel_name: str) -> list[str]:
        return sorted(self.handles[h].nickname for h in self.members(channel_name) if h in self.handles)

    def broadcast_roster(self, channel_name: str) -> None:
        """Send every member of a channel the current nickname list. Spokes cache it; it is not written to inboxes."""
        message = protocol.roster(channel_name, self.nicknames(channel_name))
        for handle in self.members(channel_name):
            self._send(handle, message)

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
                    message = protocol.loads(single)
                except ValueError as exc:
                    self.log(f"dropping malformed control frame from {source!r}: {exc}")
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
        """Bind a (channel, nickname) pair to a handle.

        Spokes send this on every DEALER connect, so it must be idempotent: after a hub changeover all of them
        re-announce at once and most are restating an identity this table already holds.
        """
        channel_name = message.get("channel")
        nickname = message.get("nickname")
        if not isinstance(channel_name, str) or not isinstance(nickname, str):
            self._send(source, protocol.error("hello needs a channel and a nickname"))
            return

        key = (channel_name, nickname)
        if (incumbent := self.by_name.get(key)) is not None and incumbent != source:
            # The name is bound to a different handle. Refuse rather than reassign, because reassigning would
            # deliver messages intended for the incumbent to the newcomer. The exception is an incumbent that no
            # longer has a live connection -- typically a session killed while its TCP connection was still open --
            # which would otherwise block the user from reusing their own nickname.
            if self._reachable(incumbent):
                self._send(source, protocol.error("nickname taken on this channel"))
                return
            self.log(f"evicted unreachable incumbent for {key!r}")

        # A handle re-announcing under a different name gives up the old one.
        if (previous := self.handles.get(source)) is not None and previous != Identity(*key):
            self.by_name.pop((previous.channel, previous.nickname), None)
            if (old := self.channels.get(previous.channel)) is not None:
                old.members.discard(source)

        identity = Identity(channel=channel_name, nickname=nickname)
        changed = self.handles.get(source) != identity

        channel = self.channels.setdefault(channel_name, ChannelInfo(name=channel_name))
        channel.members.add(source)
        self.handles[source] = identity
        self.by_name[key] = source
        self.whois_inflight.discard(source)

        # Log only identity changes: reconnect-driven hellos would otherwise repeat a line per spoke per changeover.
        if changed:
            self.log(f"hello: {nickname!r} on {channel_name!r} as {source!r}")
        self._flush_pending(source)
        self.broadcast_roster(channel_name)

    def _data(self, source: bytes, dest_frame: bytes, body: bytes) -> None:
        """Route one data message. The sender's channel is read from `handles`, not from the destination frame."""
        if (identity := self.handles.get(source)) is None:
            self._hold(source, dest_frame, body)
            return

        try:
            destination = protocol.loads(dest_frame)
        except ValueError as exc:
            self.log(f"dropping malformed destination from {source!r}: {exc}")
            return

        # `channel` in the destination frame is checked against the sender's registered channel and otherwise
        # unused. Because targets are always resolved within `identity.channel`, a spoke cannot address a channel it
        # has not joined even if it names one here.
        claimed = destination.get("channel")
        if claimed is not None and claimed != identity.channel:
            self._send(source, protocol.error(f"you are not on channel {claimed!r}"))
            return

        target_nickname = destination.get("nickname")
        message_id = protocol.new_ulid()

        if target_nickname is None:
            targets = self.members(identity.channel) - {source}
        elif (target := self.by_name.get((identity.channel, target_nickname))) is not None:
            targets = {target}
        else:
            self._send(source, protocol.bounce(message_id, "no such nickname on this channel"))
            return

        envelope = protocol.envelope(
            channel=identity.channel,
            sender=identity.nickname,
            to=target_nickname,
            body=body.decode("utf-8", errors="replace"),
            msg_id=message_id,
        )
        for handle in targets:
            if not self._send(handle, envelope) and target_nickname is not None:
                # The recipient's handle became unreachable between target lookup and send. Only direct messages
                # bounce: a broadcast that fails for one member still reached the others.
                self._send(source, protocol.bounce(message_id, "recipient went away"))

    # -- whois -----------------------------------------------------------

    def _hold(self, source: bytes, dest_frame: bytes, body: bytes) -> None:
        """Hold a message from an unregistered handle and send that handle a `whois`.

        Holding rather than bouncing means a send issued during a hub changeover is delivered late instead of
        failing, so the caller sees no error. `_flush_pending` replays these once `hello` arrives.
        """
        held = self.pending.setdefault(source, [])
        now = time.monotonic()
        held[:] = [h for h in held if now - h.at < PENDING_MAX_AGE_SECONDS]

        if len(held) >= PENDING_MAX_PER_HANDLE:
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
