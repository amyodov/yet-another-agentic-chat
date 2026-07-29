"""The backend: sockets, hub election, receive loops, and connection state.

Runs inside the MCP server process. No process is forked or daemonised, so the sockets and tasks here are torn down
when the session's server exits, and two YAAC versions can never end up talking to each other through a surviving
daemon.

One process can hold several memberships at once. Each `Membership` has its own handle, DEALER, roster and inbox, and
the hub routes to it like any other participant, so this needs no protocol support. It matters for clients that run
one MCP server per application rather than per conversation -- Claude Desktop, for instance -- where a single
membership would force every conversation to share one nickname.

The bind election is per process, not per membership: only one ROUTER can hold the endpoint, and it serves every
DEALER regardless of which process owns it.

States:

* **dormant** -- no sockets, no inbox, no tasks. `Backend` is not constructed until `join_channel` is called,
  and gives up the ROUTER again once the last membership disconnects. The server is installed in every session the
  user runs, so sessions that never join a channel must have no side effects.
* **probing** -- a single DEALER opened and closed inside `probe_channels`. It does not bind. If it did, a session
  that only called `list_channels` would become the hub and drop the endpoint when the call returned.
* **on air** -- one or more memberships, and a ROUTER as well if this process won the bind.
"""

import asyncio
import contextlib
import random
import sys
from dataclasses import dataclass
from typing import Any

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from . import protocol
from .hub import Hub
from .protocol import Address

DEFAULT_ENDPOINT = "tcp://127.0.0.1:19116"
"""19116 is 0x4AAC. Chosen below the ephemeral port range so the kernel will not assign it as the source port of an
unrelated outbound connection, which would make the bind fail for reasons unrelated to YAAC."""

BIND_RETRY_SECONDS = 2.0
HELLO_TIMEOUT_SECONDS = 5.0
PROBE_TIMEOUT_SECONDS = 10.0
SEND_HIGH_WATER_MARK = 1000
"""Outbound queue limit. Once reached, `send` raises `zmq.Again` instead of blocking or buffering without bound."""


def log(message: str) -> None:
    """Write a diagnostic line to stderr.

    stdout carries the MCP stdio transport. Any non-JSON-RPC byte written there makes the client fail to parse the
    stream, so nothing in this package may print to stdout.
    """
    print(f"[yaac] {message}", file=sys.stderr, flush=True)


class NotConnected(Exception):
    """Raised when an operation needs a membership and there is none, or the connection id is unknown."""


class AmbiguousConnection(Exception):
    """Raised when no connection id was given and this process holds more than one membership."""

    def __init__(self, open_connections: list[dict[str, Any]]) -> None:
        super().__init__("this session holds several connections; pass connection_id")
        self.open_connections = open_connections


class ConnectionRefused(Exception):
    """Raised when `connect` fails: nickname already bound on that channel, or no hub answered in time."""


@dataclass
class Connection:
    """Result of a successful `connect`.

    `connection_id` is the membership's handle. The caller passes it back to address this membership when the process
    holds more than one. `created` is True when this call was the channel's first member.
    """

    connection_id: str
    channel: str
    nickname: str
    created: bool
    peers: list[str]


class Membership:
    """One (channel, nickname) pair held by this process, with its own DEALER, roster cache and inbox."""

    def __init__(self, backend: Backend, channel: str, nickname: str) -> None:
        self.backend = backend
        self.handle = protocol.new_ulid()
        self.channel = channel
        self.nickname = nickname
        self.roster: list[Address] = []
        # Messages wait here until check_inbox collects them. In memory, so this membership leaves nothing behind
        # when the process exits, however it exits.
        self.inbox: list[dict[str, Any]] = []

        self.dealer: zmq.asyncio.Socket = backend.ctx.socket(zmq.DEALER)
        # ROUTING_ID must be set before connect(); libzmq ignores later changes. It is a generated ULID rather than
        # the nickname because a routing id is limited to 255 bytes, must not begin with a zero byte, and must be
        # unique per ROUTER, while nicknames are arbitrary user-supplied UTF-8 subject to none of those rules.
        self.dealer.setsockopt(zmq.ROUTING_ID, self.handle.encode("ascii"))
        self.dealer.setsockopt(zmq.LINGER, 0)
        self.dealer.setsockopt(zmq.SNDHWM, SEND_HIGH_WATER_MARK)
        self.monitor = self.dealer.get_monitor_socket(zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED)

        self._tasks: set[asyncio.Task] = set()
        self._hello_ack: asyncio.Future | None = None

    # -- lifecycle -------------------------------------------------------

    async def open(self) -> bool:
        """Connect and announce. Returns True if this membership brought the channel into being."""
        self.dealer.connect(self.backend.endpoint)
        self._spawn(self._pump_dealer(), f"yaac-dealer-{self.handle}")
        self._spawn(self._monitor_loop(), f"yaac-monitor-{self.handle}")

        self._hello_ack = asyncio.get_running_loop().create_future()
        try:
            await self._send_hello()
            await asyncio.wait_for(self._hello_ack, timeout=HELLO_TIMEOUT_SECONDS)
        except TimeoutError:
            raise ConnectionRefused("no answer from the hub") from None
        finally:
            self._hello_ack = None

        # The hub deletes a channel when its last member leaves, so a roster naming only this membership means the
        # channel did not exist before this call. Reported so the caller can catch a mistyped channel name.
        return [p.handle for p in self.roster] == [self.handle]

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
        await self.dealer.send(protocol.dumps(protocol.hello(self.channel, self.nickname, self.handle)))

    async def _monitor_loop(self) -> None:
        """Re-send `hello` whenever this DEALER reports EVENT_CONNECTED.

        Replaces ROUTER_NOTIFY, which is a libzmq draft option: `zmq.ROUTER_NOTIFY` imports, but `setsockopt` rejects
        it with EINVAL because released wheels bundle libzmq built without draft support. Monitoring the local socket
        also covers the initial connect, not only reconnects, so a newly elected hub is told who is present without
        having to send `whois`.
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
                    log(f"{self.nickname!r} lost the hub; the DEALER will reconnect by itself")

    async def _pump_dealer(self) -> None:
        """Receive loop. Every message the hub sends this membership is one JSON frame."""
        while True:
            try:
                frame = await self.dealer.recv()
            except zmq.ZMQError, asyncio.CancelledError:
                return
            try:
                message = protocol.parse(frame)
            except ValueError as exc:
                log(f"dropping unreadable frame from the hub: {exc}")
                continue
            self._deliver(message)

    def _deliver(self, message: dict[str, Any]) -> None:
        """Apply one message: write it to the inbox, update the roster cache, or resolve a pending hello."""
        if not protocol.is_control(message):
            self._append(message)
            return

        match message.get("kind"):
            case "whois":
                # Sent by a hub whose table does not contain this handle, typically one elected moments ago.
                self._spawn(self._send_hello(), f"yaac-whois-{self.handle}")
            case "roster":
                # Cached for `peers()`. Not written to the inbox: membership changes are not messages, and inboxing
                # them would put a line into the agent's context every time any participant reconnected.
                if message.get("channel") == self.channel:
                    self.roster = [
                        address for peer in message.get("peers", []) if (address := Address.from_wire(peer)) is not None
                    ]
                    if self._hello_ack is not None and not self._hello_ack.done():
                        self._hello_ack.set_result(True)
            case "error":
                reason = message.get("reason", "refused")
                if self._hello_ack is not None and not self._hello_ack.done():
                    self._hello_ack.set_exception(ConnectionRefused(reason))
                else:
                    self._append(message)
            case "bounce":
                # Written to the inbox so an undeliverable message is visible to the agent rather than silently lost.
                self._append(message)
            case other:
                log(f"ignoring control message {other!r} from the hub")

    def _append(self, message: dict[str, Any]) -> None:
        self.inbox.append(message)

    # -- operations ------------------------------------------------------

    async def send(self, body: str, nickname: str | None = None, handle: str | None = None) -> str:
        """Queue one message for the hub. Does not block.

        Naming neither a nickname nor a handle addresses every other member of the channel. Sent with NOBLOCK, so
        reaching SNDHWM raises `zmq.Again` rather than waiting; blocking here would stall the MCP call and with it
        the user's session.
        """
        to = Address(nickname=nickname, handle=handle) if (nickname or handle) else None
        destination = protocol.dumps(protocol.destination(self.channel, to))
        try:
            await self.dealer.send_multipart([destination, body.encode("utf-8")], zmq.NOBLOCK)
        except zmq.Again:
            raise RuntimeError("send queue is full -- the hub is not keeping up") from None
        return protocol.new_ulid()

    def receive(self) -> list[dict[str, Any]]:
        """Take everything received since the last call, emptying the inbox."""
        collected, self.inbox = self.inbox, []
        return collected

    def pending_count(self) -> int:
        """How many messages are waiting, without taking them."""
        return len(self.inbox)

    def peers(self) -> list[Address]:
        """Everyone else on this channel, from the cached roster."""
        return [p for p in self.roster if p.handle != self.handle]

    def peer_nicknames(self) -> list[str]:
        """Just the names, for display."""
        return [p.nickname for p in self.peers() if p.nickname is not None]

    def describe(self) -> dict[str, Any]:
        return {
            "connection_id": self.handle,
            "channel": self.channel,
            "nickname": self.nickname,
            "unread": self.pending_count(),
        }


class Backend:
    """Process-level transport: the ZMQ context, the bind election, and every membership this process holds."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self.endpoint = endpoint
        self.ctx = zmq.asyncio.Context()
        self.router: zmq.asyncio.Socket | None = None
        self.hub: Hub | None = None
        self.memberships: dict[str, Membership] = {}
        self._tasks: set[asyncio.Task] = set()

    # -- state -----------------------------------------------------------

    @property
    def on_air(self) -> bool:
        return bool(self.memberships)

    @property
    def is_hub(self) -> bool:
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
        """Query the hub for the list of occupied channels, then close the socket.

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
            await probe.send(protocol.dumps(protocol.channels_query()))
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while (remaining := deadline - loop.time()) > 0:
                try:
                    frame = await asyncio.wait_for(probe.recv(), timeout=remaining)
                except TimeoutError:
                    return None
                with contextlib.suppress(ValueError):
                    if (message := protocol.parse(frame)).get("kind") == "channels":
                        return message.get("channels", [])
            return None
        finally:
            probe.close()

    # -- memberships -----------------------------------------------------

    async def connect(self, channel: str, nickname: str) -> Connection:
        """Join `channel` as `nickname` and return the connection id used to address this membership later.

        `nickname` is supplied by the caller and is never derived from the working directory, hostname, or any other
        ambient value.

        Raises ConnectionRefused if this process already holds that exact membership, if the nickname is bound to a
        live handle on that channel, or if no hub answers within HELLO_TIMEOUT_SECONDS.
        """
        for existing in self.memberships.values():
            if (existing.channel, existing.nickname) == (channel, nickname):
                raise ConnectionRefused(f"already on {channel!r} as {nickname!r}")

        membership = Membership(self, channel, nickname)
        self._ensure_election_running()
        self._try_bind()
        try:
            created = await membership.open()
        except BaseException:
            await membership.close()
            self._release_if_idle()
            raise

        self.memberships[membership.handle] = membership
        log(f"on air as {nickname!r} on {channel!r} ({'hub' if self.is_hub else 'spoke'})")
        return Connection(
            connection_id=membership.handle,
            channel=channel,
            nickname=nickname,
            created=created,
            peers=membership.peer_nicknames(),
        )

    async def disconnect(self, connection_id: str | None = None) -> Membership:
        """Close one membership and return it. Gives up the ROUTER when the last one goes."""
        membership = self.resolve(connection_id)
        del self.memberships[membership.handle]
        await membership.close()
        log(f"off air ({membership.nickname!r} on {membership.channel!r})")
        self._release_if_idle()
        return membership

    async def disconnect_all(self) -> None:
        for connection_id in list(self.memberships):
            await self.disconnect(connection_id)

    def describe_all(self) -> list[dict[str, Any]]:
        return [m.describe() for m in self.memberships.values()]

    def total_unread(self) -> int:
        return sum(m.pending_count() for m in self.memberships.values())

    # -- hub election ----------------------------------------------------

    def _try_bind(self) -> bool:
        """Try to bind the endpoint, becoming the hub on success.

        Binding an occupied port returns EADDRINUSE immediately -- measured at 0.4 ms -- so every process can attempt
        it unconditionally and exactly one succeeds. No coordination between participants is required.

        libzmq sets SO_REUSEADDR on its listening sockets, so a TIME_WAIT entry left by a previous hub does not
        prevent the next bind.
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
                log(f"bind failed unexpectedly: {exc}")
            return False
        self.router = router
        self.hub = Hub(router, log)
        # Every membership's DEALER is connected to this endpoint, so the hub reaches its own process through its own
        # ROUTER like any other participant. This keeps one send path and one receive path regardless of role.
        self._spawn(self._pump_router(), "yaac-router")
        log(f"won the bind: this session is now the hub on {self.endpoint}")
        return True

    def _ensure_election_running(self) -> None:
        if not any(t.get_name() == "yaac-election" for t in self._tasks):
            self._spawn(self._election_loop(), "yaac-election")

    async def _election_loop(self) -> None:
        """Retry the bind every BIND_RETRY_SECONDS (jittered) so a departed hub is replaced without user action.

        No other failover logic is needed on a spoke: libzmq reconnects each DEALER, the ROUTING_ID is unchanged, and
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
        while self.router is not None and self.hub is not None:
            try:
                frames = await self.router.recv_multipart()
            except zmq.ZMQError, asyncio.CancelledError:
                return
            try:
                self.hub.handle_frames(frames)
            except Exception as exc:
                # One malformed or unroutable message must not end the loop, or the endpoint would stay bound with
                # nothing servicing it and no other process able to take over.
                log(f"hub error while routing: {exc!r}")

    def _release_if_idle(self) -> None:
        """Give up the ROUTER and the election loop once no membership is left, so a dormant process holds nothing."""
        if self.memberships:
            return
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self.router is not None:
            self.router.close()
            self.router = None
            self.hub = None
            log("released the bind")

    def close(self) -> None:
        """Terminate the ZMQ context. Call after disconnecting, on process shutdown."""
        self.ctx.term()


def check_zmq_capabilities() -> None:
    """Raise if the installed pyzmq lacks a socket option this implementation requires.

    ROUTER_NOTIFY is deliberately not checked: it is a draft option absent from every released wheel, and nothing
    here uses it. See the `hub` module docstring and `Membership._monitor_loop` for the mechanisms used instead.
    """
    if missing := [n for n in ("ROUTER_MANDATORY", "ROUTER_HANDOVER") if not hasattr(zmq, n)]:
        raise RuntimeError(
            f"this pyzmq lacks {', '.join(missing)}; libzmq 4.2+ is required (found libzmq {zmq.zmq_version()})"
        )
