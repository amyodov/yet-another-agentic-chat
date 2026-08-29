"""The backend: sockets, the hat, receive loops, and connection state.

Runs inside the MCP server process. No process is forked or daemonised, so the sockets and tasks here are torn down
when the session's server exits, and two YAAC versions can never end up talking to each other through a surviving
daemon.

One process can hold several memberships at once. Each `Membership` has its own routing id, DEALER, roster and
inbox, and the hat routes to it like any other participant, so this needs no protocol support. It matters for
clients running one MCP server per application rather than per conversation -- Claude Desktop, for instance -- where
a single membership would force every conversation to share one name.

The bind election is per process, not per membership: only one ROUTER can hold the endpoint, and it serves every
DEALER regardless of which process owns it.

States:

* **dormant** -- no sockets, no inbox, no tasks. `Backend` is not constructed until `join_channel` is called,
  and gives up the ROUTER again once the last membership disconnects. The server is installed in every session the
  user runs, so sessions that never join a channel must have no side effects.
* **probing** -- a single DEALER opened and closed inside `probe_channels`. It does not bind. If it did, a session
  that only called `list_channels` would put on the hat and drop the endpoint when the call returned.
* **on air** -- one or more memberships, and a ROUTER as well if this process won the bind.
"""

import asyncio
import contextlib
import logging
import os
import random
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from . import protocol
from .hat import Hat
from .notices import Notices, describe_arrival
from .protocol import Address

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = "tcp://127.0.0.1:19116"
"""19116 is 0x4AAC. Chosen below the ephemeral port range so the kernel will not assign it as the source port of an
unrelated outbound connection, which would make the bind fail for reasons unrelated to YAAC."""

BIND_RETRY_SECONDS = 2.0
HELLO_TIMEOUT_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 10.0
SEND_HIGH_WATER_MARK = 1000
"""Outbound queue limit. Once reached, `send` raises `zmq.Again` instead of blocking or buffering without bound."""


def configure_logging() -> None:
    """Send this package's log to stderr. Called by the entry points, never on import.

    stdout carries the MCP stdio transport, so a handler that defaulted there would break the session with a parse
    error -- which is why the handler is explicit rather than left to `basicConfig`, whose default is stderr today
    but is not ours to assume. A library that configured logging on import would also be deciding this for whoever
    imported it.

    `YAAC_LOG_LEVEL` names any level `logging` knows; the default says what a session did without narrating it.
    """
    logger = logging.getLogger(__package__)
    logger.setLevel(os.environ.get("YAAC_LOG_LEVEL", "INFO").upper())
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[yaac] %(message)s"))
    logger.addHandler(handler)
    # Nothing above this package should have to care, and a root handler installed by a host would otherwise get a
    # second copy of every line.
    logger.propagate = False


class NotConnected(Exception):
    """Raised when an operation needs a membership and there is none, or the connection id is unknown."""


class AmbiguousConnection(Exception):
    """Raised when no connection id was given and this process holds more than one membership."""

    def __init__(self, open_connections: list[dict[str, Any]]) -> None:
        super().__init__("this session holds several connections; pass connection_id")
        self.open_connections = open_connections


class ConnectionRefused(Exception):
    """Raised when `connect` fails: name already bound on that channel, or no hat answered in time."""


@dataclass
class Connection:
    """Result of a successful `connect`.

    `connection_id` is the membership's routing id. The caller passes it back to address this membership when the
    process holds more than one. `created` is True when this call was the channel's first member.
    """

    connection_id: str
    channel: str
    name: str
    created: bool
    peers: list[str]


class Membership:
    """One (channel, name) pair held by this process, with its own DEALER, roster cache and inbox."""

    def __init__(self, backend: Backend, channel: str, name: str) -> None:
        self.backend = backend
        self.routing_id = protocol.new_ulid()
        self.channel = channel
        self.name = name
        self.roster: list[Address] = []
        # Messages wait here until check_inbox collects them. In memory, so this membership leaves nothing behind
        # when the process exits, however it exits.
        self.inbox: list[dict[str, Any]] = []

        self.dealer: zmq.asyncio.Socket = backend.ctx.socket(zmq.DEALER)
        # ROUTING_ID must be set before connect(); libzmq ignores later changes. It is a generated ULID rather than
        # the name because a routing id is limited to 255 bytes, must not begin with a zero byte, and must be
        # unique per ROUTER, while names are arbitrary user-supplied UTF-8 subject to none of those rules.
        self.dealer.setsockopt(zmq.ROUTING_ID, self.routing_id.encode("ascii"))
        self.dealer.setsockopt(zmq.LINGER, 0)
        self.dealer.setsockopt(zmq.SNDHWM, SEND_HIGH_WATER_MARK)
        self.monitor = self.dealer.get_monitor_socket(zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED)

        self._tasks: set[asyncio.Task] = set()
        self._hello_ack: asyncio.Future | None = None
        # Called whenever the inbox or the roster changes, so a live view can redraw without polling for it. The MCP
        # frontend leaves this None: nothing can be pushed into an idle model session, so there is nothing to notify.
        self.on_change: Callable[[], None] | None = None

    # -- lifecycle -------------------------------------------------------

    async def open(self) -> bool:
        """Connect and announce. Returns True if this membership brought the channel into being."""
        self.dealer.connect(self.backend.endpoint)
        self._spawn(self._pump_dealer(), f"yaac-dealer-{self.routing_id}")
        self._spawn(self._monitor_loop(), f"yaac-monitor-{self.routing_id}")

        self._hello_ack = asyncio.get_running_loop().create_future()
        try:
            await self._send_hello()
            await asyncio.wait_for(self._hello_ack, timeout=HELLO_TIMEOUT_SECONDS)
        except TimeoutError:
            raise ConnectionRefused("no answer from the hat") from None
        finally:
            self._hello_ack = None

        # The hat deletes a channel when its last member leaves, so a roster naming only this membership means the
        # channel did not exist before this call. Reported so the caller can catch a mistyped channel name.
        return [p.routing_id for p in self.roster] == [self.routing_id]

    async def close(self) -> None:
        """Cancel this membership's tasks and close its sockets."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

        for sock in (self.monitor, self.dealer):
            sock.close()
        self.inbox.clear()

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- loops -----------------------------------------------------------

    async def _send_hello(self) -> None:
        await self.dealer.send(protocol.dumps(protocol.hello(self.channel, self.name, self.routing_id).to_wire()))

    async def _monitor_loop(self) -> None:
        """Re-send `hello` whenever this DEALER reports EVENT_CONNECTED.

        Replaces ROUTER_NOTIFY, which is a libzmq draft option: `zmq.ROUTER_NOTIFY` imports, but `setsockopt` rejects
        it with EINVAL because released wheels bundle libzmq built without draft support. Monitoring the local socket
        also covers the initial connect, not only reconnects, so whoever picks up the hat next is told who is present
        without having to send `whois`.
        """
        while True:
            try:
                event = parse_monitor_message(await self.monitor.recv_multipart())
            except zmq.ZMQError, asyncio.CancelledError:
                return
            match event["event"]:
                case zmq.EVENT_CONNECTED:
                    with contextlib.suppress(zmq.ZMQError):
                        await self._send_hello()
                case zmq.EVENT_DISCONNECTED:
                    logger.info("%r lost the hat; the DEALER will reconnect by itself", self.name)

    async def _pump_dealer(self) -> None:
        """Receive loop. Every message the hat sends this membership is one JSON frame."""
        while True:
            try:
                frame = await self.dealer.recv()
            except zmq.ZMQError, asyncio.CancelledError:
                return
            try:
                envelope = protocol.Envelope.from_wire(protocol.parse(frame))
            except ValueError as exc:
                # The version is named because a mismatch is otherwise silent: an endpoint that accepts
                # connections and answers nothing reads exactly like an empty network.
                logger.warning("dropping unreadable frame from the hat (protocol %s): %s",
                               protocol.peek_version(frame), exc)
                continue
            self._deliver(envelope)

    def _deliver(self, envelope: protocol.Envelope) -> None:
        """Apply one message: write it to the inbox, update the roster cache, or resolve a pending hello.

        Operator mail is what arrives stamped `from: {}` -- the hat speaking as itself rather than carrying
        somebody. What this side does with it is backend policy, not a second format: a roster goes to the cache,
        a bounce goes to the inbox.
        """
        if envelope.frm is None or not envelope.frm.is_the_hat:
            self._append(envelope.to_wire())
            return

        payload = envelope.payload if isinstance(envelope.payload, dict) else {}
        match envelope.op:
            case "whois":
                # Sent by a hat whose table does not contain this routing id, typically one elected moments ago.
                self._spawn(self._send_hello(), f"yaac-whois-{self.routing_id}")
            case "roster":
                # Cached for `peers()`. Not written to the inbox: membership changes are not messages, and inboxing
                # them would put a line into the agent's context every time any participant reconnected.
                if payload.get("channel") == self.channel:
                    self.roster = [
                        address
                        for peer in payload.get("members", [])
                        if (address := Address.from_wire(peer)) is not None
                    ]
                    self._changed()
                    if self._hello_ack is not None and not self._hello_ack.done():
                        self._hello_ack.set_result(True)
            case "error":
                reason = payload.get("reason", "refused")
                if self._hello_ack is not None and not self._hello_ack.done():
                    self._hello_ack.set_exception(ConnectionRefused(reason))
                else:
                    self._append(envelope.to_wire())
            case "bounce":
                # Written to the inbox so an undeliverable message is visible to the agent rather than lost.
                self._append(envelope.to_wire())
            case other:
                logger.warning("ignoring operator message %r from the hat", other)

    def _append(self, message: dict[str, Any]) -> None:
        self.inbox.append(message)
        # Told to whoever is watching from outside this process, because nothing else can see an inbox move.
        self.backend.notices.announce(describe_arrival(self.channel, self.name, message))
        self._changed()

    def _changed(self) -> None:
        """Tell a live view that this membership moved. A broken observer must not take the receive loop with it."""
        if self.on_change is None:
            return
        try:
            self.on_change()
        except Exception as exc:  # noqa: BLE001 -- an observer is a guest here, not part of the transport
            logger.warning("on_change observer raised %r; continuing", exc)

    # -- operations ------------------------------------------------------

    async def send(
        self,
        body: str,
        name: str | None = None,
        routing_id: str | None = None,
        payload: Any = None,
        mentions: tuple[Address, ...] = (),
        tags: tuple[str, ...] = (),
    ) -> str:
        """Queue one message for the hat. Does not block.

        Naming neither a name nor a routing_id addresses every other member of the channel. Sent with NOBLOCK, so
        reaching SNDHWM raises `zmq.Again` rather than waiting; blocking here would stall the MCP call and with it
        the user's session.

        `from` is not written: the hat stamps it from its own table, which is the whole of hard rule 6.
        """
        peer = Address(name=name, routing_id=routing_id) if (name or routing_id) else None
        envelope = protocol.message(
            protocol.Scope(channel=self.channel, peer=peer),
            body=body,
            payload=payload,
            mentions=mentions,
            tags=tags,
        )
        try:
            await self.dealer.send(protocol.dumps(envelope.to_wire()), zmq.NOBLOCK)
        except zmq.Again:
            raise RuntimeError("send queue is full -- the hat is not keeping up") from None
        return envelope.id

    def receive(self) -> list[dict[str, Any]]:
        """Take everything received since the last call, emptying the inbox."""
        collected, self.inbox = self.inbox, []
        return collected

    def pending_count(self) -> int:
        """How many messages are waiting, without taking them."""
        return len(self.inbox)

    def peers(self) -> list[Address]:
        """Everyone else on this channel, from the cached roster."""
        return [p for p in self.roster if p.routing_id != self.routing_id]

    def peer_names(self) -> list[str]:
        """Just the names, for display."""
        return [p.name for p in self.peers() if p.name is not None]

    def describe(self) -> dict[str, Any]:
        return {
            "connection_id": self.routing_id,
            "channel": self.channel,
            "name": self.name,
            "unread": self.pending_count(),
        }


class Backend:
    """Process-level transport: the ZMQ context, the bind election, and every membership this process holds."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self.endpoint = endpoint
        self.ctx = zmq.asyncio.Context()
        self.router: zmq.asyncio.Socket | None = None
        self.hat: Hat | None = None
        self.memberships: dict[str, Membership] = {}
        self._tasks: set[asyncio.Task] = set()
        # Opened with the first membership and closed with the last, so a dormant process still listens on nothing.
        # Notices are an extra: everything works without a reader, and nothing here is on the delivery path.
        self.notices = Notices()
        self.notices.snapshot = self.describe_all

    # -- state -----------------------------------------------------------

    @property
    def on_air(self) -> bool:
        return bool(self.memberships)

    @property
    def is_wearing_hat(self) -> bool:
        return self.router is not None

    def resolve(self, connection_id: str | None) -> Membership:
        """Find the membership a call refers to.

        With exactly one membership open, `connection_id` may be omitted, which keeps the common case free of
        bookkeeping. With several open it is required, because guessing would deliver to the wrong channel.
        """
        if not self.memberships:
            raise NotConnected("not connected -- call join_channel first")
        if connection_id is None:
            if len(self.memberships) > 1:
                raise AmbiguousConnection(self.describe_all())
            return next(iter(self.memberships.values()))
        if (membership := self.memberships.get(connection_id)) is None:
            raise NotConnected(f"no open connection with id {connection_id!r}")
        return membership

    def _spawn(self, coro, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- probing ---------------------------------------------------------

    async def probe_channels(self, timeout: float = PROBE_TIMEOUT_SECONDS) -> list[dict] | None:
        """Query the hat for the list of occupied channels, then close the socket.

        Opens a DEALER, sends `channels?`, waits for a `channels` reply, and closes. Does not bind.

        Returns None if no reply arrives within `timeout`, which is how an unoccupied endpoint is reported. The
        timeout is the only exit for that case: `connect()` on a TCP endpoint with no listener does not raise, and
        the DEALER queues the request until a peer appears.
        """
        probe = self.ctx.socket(zmq.DEALER)
        probe.setsockopt(zmq.LINGER, 0)
        probe.setsockopt(zmq.SNDHWM, SEND_HIGH_WATER_MARK)
        probe.setsockopt(zmq.ROUTING_ID, protocol.new_ulid().encode("ascii"))
        try:
            probe.connect(self.endpoint)
            await probe.send(protocol.dumps(protocol.channels_query().to_wire()))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while (remaining := deadline - loop.time()) > 0:
                try:
                    frame = await asyncio.wait_for(probe.recv(), timeout=remaining)
                except TimeoutError:
                    return None
                with contextlib.suppress(ValueError):
                    answer = protocol.Envelope.from_wire(protocol.parse(frame))
                    # `to: {}` asked; `from: {}` answers. Direction is what tells the question from the reply.
                    if answer.op == "channels" and answer.frm is not None and answer.frm.is_the_hat:
                        payload = answer.payload if isinstance(answer.payload, dict) else {}
                        return payload.get("channels", [])
            return None
        finally:
            probe.close()

    # -- memberships -----------------------------------------------------

    async def connect(self, channel: str, name: str) -> Connection:
        """Join `channel` as `name` and return the connection id used to address this membership later.

        `name` is supplied by the caller and is never derived from the working directory, hostname, or any other
        ambient value.

        Raises ConnectionRefused if this process already holds that exact membership, if the name is bound to a
        live routing_id on that channel, or if no hat answers within HELLO_TIMEOUT_SECONDS.
        """
        for existing in self.memberships.values():
            if (existing.channel, existing.name) == (channel, name):
                raise ConnectionRefused(f"already on {channel!r} as {name!r}")

        membership = Membership(self, channel, name)
        self._ensure_election_running()
        self._try_bind()
        try:
            created = await membership.open()
        except BaseException:
            await membership.close()
            self._release_if_idle()
            raise

        self.memberships[membership.routing_id] = membership
        await self.notices.start()
        logger.info("on air as %r on %r (%s)", name, channel, "hat" if self.is_wearing_hat else "participant")
        return Connection(
            connection_id=membership.routing_id,
            channel=channel,
            name=name,
            created=created,
            peers=membership.peer_names(),
        )

    async def disconnect(self, connection_id: str | None = None) -> Membership:
        """Close one membership and return it. Gives up the ROUTER when the last one goes."""
        membership = self.resolve(connection_id)
        del self.memberships[membership.routing_id]
        await membership.close()
        logger.info("off air (%r on %r)", membership.name, membership.channel)
        self._release_if_idle()
        return membership

    async def disconnect_all(self) -> None:
        for connection_id in list(self.memberships):
            await self.disconnect(connection_id)

    def describe_all(self) -> list[dict[str, Any]]:
        return [m.describe() for m in self.memberships.values()]

    def total_unread(self) -> int:
        return sum(m.pending_count() for m in self.memberships.values())

    # -- hat election ----------------------------------------------------

    def _try_bind(self) -> bool:
        """Try to bind the endpoint, putting the hat on if it succeeds.

        Binding an occupied port returns EADDRINUSE immediately -- measured at 0.4 ms -- so every process can attempt
        it unconditionally and exactly one succeeds. No coordination between participants is required.

        libzmq sets SO_REUSEADDR on its listening sockets, so a TIME_WAIT entry left by a previous hat does not
        prevent the next bind. On Windows it sets SO_EXCLUSIVEADDRUSE instead (tcp_listener.cpp), because there
        SO_REUSEADDR would let a second bind of an actively-bound port succeed -- and the election would crown two
        hats.
        """
        if self.router is not None:
            return True
        router = self.ctx.socket(zmq.ROUTER)
        router.setsockopt(zmq.ROUTER_MANDATORY, 1)  # unknown destination raises instead of dropping
        router.setsockopt(zmq.ROUTER_HANDOVER, 1)  # a reconnecting peer reclaims its routing id
        router.setsockopt(zmq.LINGER, 0)
        try:
            router.bind(self.endpoint)
        except zmq.ZMQError as exc:
            router.close()
            if exc.errno != zmq.EADDRINUSE:
                logger.warning("bind failed unexpectedly: %s", exc)
            return False
        self.router = router
        self.hat = Hat(router)
        # Every membership's DEALER is connected to this endpoint, so the hat reaches its own process through its own
        # ROUTER like any other participant. This keeps one send path and one receive path regardless of role.
        self._spawn(self._pump_router(), "yaac-router")
        logger.info("won the bind: this session is now wearing the hat on %s", self.endpoint)
        return True

    def _ensure_election_running(self) -> None:
        if not any(t.get_name() == "yaac-election" for t in self._tasks):
            self._spawn(self._election_loop(), "yaac-election")

    async def _election_loop(self) -> None:
        """Retry the bind every BIND_RETRY_SECONDS (jittered) so a departed hat is replaced without user action.

        No other failover logic is needed on a participant: libzmq reconnects each DEALER, the ROUTING_ID is unchanged,
        and
        `Membership._monitor_loop` re-sends `hello` to whichever process now holds the endpoint.
        """
        while True:
            await asyncio.sleep(BIND_RETRY_SECONDS * (0.75 + random.random() * 0.5))
            if self.router is None and self._try_bind():
                for membership in self.memberships.values():
                    with contextlib.suppress(zmq.ZMQError):
                        await membership._send_hello()

    async def _pump_router(self) -> None:
        """Receive loop for the ROUTER. Started by `_try_bind` only on the process that holds the endpoint."""
        while self.router is not None and self.hat is not None:
            try:
                frames = await self.router.recv_multipart()
            except zmq.ZMQError, asyncio.CancelledError:
                return
            try:
                self.hat.handle_frames(frames)
            except Exception as exc:
                # One malformed or unroutable message must not end the loop, or the endpoint would stay bound with
                # nothing servicing it and no other process able to take over.
                logger.warning("hat error while routing: %r", exc)

    def _release_if_idle(self) -> None:
        """Give up the ROUTER and the election loop once no membership is left, so a dormant process holds nothing."""
        if self.memberships:
            return
        # Spawned rather than awaited: this runs from synchronous paths, and a closing listener has nobody waiting.
        if self.notices.port is not None:
            asyncio.ensure_future(self.notices.stop())
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self.router is not None:
            self.router.close()
            self.router = None
            self.hat = None
            logger.info("released the bind")

    def close(self) -> None:
        """Close every socket, then terminate the context. Call on process shutdown.

        `destroy` rather than `term`: `term` waits for the sockets in the context to be closed, and on the path
        that matters nothing has closed them. A client going away ends the stdio loop with memberships still open,
        so `term` blocks in a process whose event loop has already stopped -- alive, holding the rendezvous port,
        and answering nothing, which is a dead net for every session on the machine until somebody notices.
        Measured: the server survived its client by three days and eighteen hours before it was killed by hand.

        LINGER is 0 on every socket this class makes, so the argument here only covers a socket made elsewhere.
        """
        self.ctx.destroy(linger=0)


def check_zmq_capabilities() -> None:
    """Raise if the installed pyzmq lacks a socket option this implementation requires.

    ROUTER_NOTIFY is deliberately not checked: it is a draft option absent from every released wheel, and nothing
    here uses it. See the `hat` module docstring and `Membership._monitor_loop` for the mechanisms used instead.
    """
    if missing := [n for n in ("ROUTER_MANDATORY", "ROUTER_HANDOVER") if not hasattr(zmq, n)]:
        raise RuntimeError(
            f"this pyzmq lacks {', '.join(missing)}; libzmq 4.2+ is required (found libzmq {zmq.zmq_version()})"
        )
