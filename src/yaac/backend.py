"""The backend: sockets, hub election, receive loops, and connection state.

Runs inside the MCP server process. No process is forked or daemonised, so the sockets and tasks here are torn down
when the session's server exits, and two YAAC versions can never end up talking to each other through a surviving
daemon.

Three states:

* **dormant** -- no sockets, no inbox, no tasks. `Backend` is not instantiated at all until `connect_to_channel` is
  called; `frontend.radio()` constructs it lazily. This matters because the server is installed in every session the
  user runs, and all of those that never join a channel must have no side effects.
* **probing** -- a single DEALER opened and closed inside `probe_channels`. It does not bind. If it did, a session
  that only called `list_channels` would become the hub and drop the endpoint when the call returned.
* **on air** -- a DEALER held open, an inbox on disk, background tasks running, and a ROUTER as well if this backend
  won the bind.
"""

import asyncio
import contextlib
import os
import random
import sys
from dataclasses import dataclass
from typing import Any

import zmq
import zmq.asyncio
from zmq.utils.monitor import parse_monitor_message

from . import protocol
from .hub import Hub
from .inbox import Inbox

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
    """Raised by `send`, `receive` and `peers` when called before `connect`."""


class ConnectionRefused(Exception):
    """Raised when `connect` fails: nickname already bound on the channel, no reply from a hub, or already on air."""


@dataclass
class Connection:
    """Result of a successful `connect`. `created` is True when this call was the channel's first member."""

    channel: str
    nickname: str
    handle: str
    created: bool
    peers: list[str]


class Backend:
    """One participant's transport. Instantiated only when the session joins a channel."""

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT) -> None:
        self.endpoint = endpoint
        self.ctx = zmq.asyncio.Context()
        self.dealer: zmq.asyncio.Socket | None = None
        self.monitor: zmq.asyncio.Socket | None = None
        self.router: zmq.asyncio.Socket | None = None
        self.hub: Hub | None = None
        self.inbox: Inbox | None = None

        self.handle: str | None = None
        self.channel: str | None = None
        self.nickname: str | None = None
        self.roster: list[str] = []

        self._tasks: set[asyncio.Task] = set()
        self._hello_ack: asyncio.Future | None = None

    # -- state -----------------------------------------------------------

    @property
    def on_air(self) -> bool:
        return self.handle is not None

    @property
    def is_hub(self) -> bool:
        return self.router is not None

    def _require_on_air(self) -> None:
        if not self.on_air:
            raise NotConnected("not connected -- call connect_to_channel first")

    def _spawn(self, coro, name: str) -> asyncio.Task:
        """Track a background task so disconnect can cancel it."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # -- probing (dormant) -----------------------------------------------

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
                    if (message := protocol.loads(frame)).get("kind") == "channels":
                        return message.get("channels", [])
            return None
        finally:
            probe.close()

    # -- going on air ----------------------------------------------------

    async def connect(self, channel: str, nickname: str) -> Connection:
        """Join `channel` as `nickname`: open the DEALER, attempt the bind, announce, and create the inbox.

        The only transition out of the dormant state. `nickname` is supplied by the caller and is never derived from
        the working directory, hostname, or any other ambient value.

        Raises ConnectionRefused if already on air, if the nickname is bound to a live handle on that channel, or if
        no hub answers within HELLO_TIMEOUT_SECONDS.
        """
        if self.on_air:
            raise ConnectionRefused(f"already on air as {self.nickname!r} on {self.channel!r}; disconnect first")

        self.handle = protocol.new_ulid()
        self.channel = channel
        self.nickname = nickname

        self.dealer = self.ctx.socket(zmq.DEALER)
        # ROUTING_ID must be set before connect(); libzmq ignores later changes. It is a generated ULID rather than
        # the nickname because a routing id is limited to 255 bytes, must not begin with a zero byte, and must be
        # unique per ROUTER, while nicknames are arbitrary user-supplied UTF-8 subject to none of those rules.
        self.dealer.setsockopt(zmq.ROUTING_ID, self.handle.encode("ascii"))
        self.dealer.setsockopt(zmq.LINGER, 0)
        self.dealer.setsockopt(zmq.SNDHWM, SEND_HIGH_WATER_MARK)
        self.monitor = self.dealer.get_monitor_socket(zmq.EVENT_CONNECTED | zmq.EVENT_DISCONNECTED)
        self.dealer.connect(self.endpoint)

        self._spawn(self._pump_dealer(), "yaac-dealer")
        self._spawn(self._monitor_loop(), "yaac-monitor")
        self._spawn(self._election_loop(), "yaac-election")
        self._try_bind()

        try:
            await self._say_hello()
        except BaseException:
            await self.disconnect()
            raise

        self.inbox = Inbox(self.handle)
        self.inbox.create(
            {
                "handle": self.handle,
                "channel": channel,
                "nickname": nickname,
                "pid": os.getpid(),
                "cwd": os.getcwd(),
                # Present under Claude Code, absent under Claude Desktop and most
                # other clients. Recorded for a future out-of-process consumer;
                # nothing in v0 depends on it.
                "session_id": os.environ.get("CLAUDE_CODE_SESSION_ID"),
            }
        )

        # The hub deletes a channel when its last member leaves, so a roster containing only this nickname means
        # the channel did not exist before this call. Reported so the caller can catch a mistyped channel name.
        created = self.roster == [nickname]
        log(f"on air as {nickname!r} on {channel!r} ({'hub' if self.is_hub else 'spoke'})")
        return Connection(
            channel=channel,
            nickname=nickname,
            handle=self.handle,
            created=created,
            peers=[p for p in self.roster if p != nickname],
        )

    async def _say_hello(self) -> None:
        """Send `hello` and wait for the resulting `roster` or `error`, which `_deliver` resolves onto the future."""
        self._hello_ack = asyncio.get_running_loop().create_future()
        try:
            await self._send_hello()
            await asyncio.wait_for(self._hello_ack, timeout=HELLO_TIMEOUT_SECONDS)
        except TimeoutError:
            raise ConnectionRefused("no answer from the hub") from None
        finally:
            self._hello_ack = None

    async def _send_hello(self) -> None:
        if self.dealer is None or self.channel is None or self.nickname is None:
            return
        await self.dealer.send(protocol.dumps(protocol.hello(self.channel, self.nickname, self.handle or "")))

    # -- hub election ----------------------------------------------------

    def _try_bind(self) -> bool:
        """Try to bind the endpoint, becoming the hub on success.

        Binding an occupied port returns EADDRINUSE immediately -- measured at 0.4 ms -- so every backend can attempt
        it unconditionally and exactly one succeeds. No coordination between participants is required.

        libzmq sets SO_REUSEADDR on its listening sockets, so a TIME_WAIT entry left by a previous hub does not
        prevent the next bind.
        """
        if self.router is not None:
            return True
        router = self.ctx.socket(zmq.ROUTER)
        router.setsockopt(zmq.ROUTER_MANDATORY, 1)  # unknown destination raises
        router.setsockopt(zmq.ROUTER_HANDOVER, 1)  # a reconnecting peer reclaims its handle
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
        # The DEALER is already connected to this endpoint, so the hub reaches itself through its own ROUTER like
        # any other participant. This keeps one send path and one receive path regardless of which backend is hub.
        self._spawn(self._pump_router(), "yaac-router")
        log(f"won the bind: this session is now the hub on {self.endpoint}")
        return True

    async def _election_loop(self) -> None:
        """Retry the bind every BIND_RETRY_SECONDS (jittered) so a departed hub is replaced without user action.

        No other failover logic is needed on a spoke: libzmq reconnects the DEALER, the ROUTING_ID is unchanged, and
        `_monitor_loop` re-sends `hello` to whichever backend now holds the endpoint.
        """
        while True:
            await asyncio.sleep(BIND_RETRY_SECONDS * (0.75 + random.random() * 0.5))
            if self.router is None and self._try_bind():
                await self._send_hello()

    # -- loops -----------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Re-send `hello` whenever the DEALER reports EVENT_CONNECTED.

        Replaces ROUTER_NOTIFY, which is a libzmq draft option: `zmq.ROUTER_NOTIFY` imports, but `setsockopt` rejects
        it with EINVAL because released wheels bundle libzmq built without draft support. Monitoring the local socket
        also covers the initial connect, not only reconnects, so a newly elected hub is told who is present without
        having to send `whois`.
        """
        while self.monitor is not None:
            try:
                event = parse_monitor_message(await self.monitor.recv_multipart())
            except zmq.ZMQError, asyncio.CancelledError:
                return
            match event["event"]:
                case zmq.EVENT_CONNECTED:
                    log("connected to the hub; re-announcing")
                    with contextlib.suppress(zmq.ZMQError):
                        await self._send_hello()
                case zmq.EVENT_DISCONNECTED:
                    log("lost the hub; the DEALER will reconnect by itself")

    async def _pump_dealer(self) -> None:
        """Receive loop for the DEALER. Every message the hub sends this participant is one JSON frame."""
        while self.dealer is not None:
            try:
                frame = await self.dealer.recv()
            except zmq.ZMQError, asyncio.CancelledError:
                return
            try:
                message = protocol.loads(frame)
            except ValueError as exc:
                log(f"dropping malformed frame from the hub: {exc}")
                continue
            self._deliver(message)

    async def _pump_router(self) -> None:
        """Receive loop for the ROUTER. Started by `_try_bind` only on the backend that holds the endpoint."""
        while self.router is not None and self.hub is not None:
            try:
                frames = await self.router.recv_multipart()
            except zmq.ZMQError, asyncio.CancelledError:
                return
            try:
                self.hub.handle_frames(frames)
            except Exception as exc:
                # One malformed or unroutable message must not end the loop, or the endpoint would stay bound with
                # nothing servicing it and no other backend able to take over.
                log(f"hub error while routing: {exc!r}")

    def _deliver(self, message: dict[str, Any]) -> None:
        """Apply one message received from the hub: inbox it, update the roster cache, or resolve a pending hello."""
        if not protocol.is_control(message):
            self._inbox_append(message)
            return

        match message.get("kind"):
            case "whois":
                # Sent by a hub whose table does not contain this handle, typically one elected moments ago.
                self._spawn(self._send_hello(), "yaac-whois-reply")
            case "roster":
                # Cached for `peers()`. Not written to the inbox: membership changes are not messages, and inboxing
                # them would put a line into the agent's context every time any participant reconnected.
                if message.get("channel") == self.channel:
                    self.roster = list(message.get("peers", []))
                    if self._hello_ack is not None and not self._hello_ack.done():
                        self._hello_ack.set_result(True)
            case "error":
                reason = message.get("reason", "refused")
                if self._hello_ack is not None and not self._hello_ack.done():
                    self._hello_ack.set_exception(ConnectionRefused(reason))
                else:
                    self._inbox_append(message)
            case "bounce":
                # Inboxed so an undeliverable message is visible to the agent rather than silently lost.
                self._inbox_append(message)
            case other:
                log(f"ignoring control message {other!r} from the hub")

    def _inbox_append(self, message: dict[str, Any]) -> None:
        if self.inbox is not None:
            self.inbox.append(message)

    # -- on-air operations -----------------------------------------------

    async def send(self, body: str, nickname: str | None = None) -> str:
        """Queue one message for the hub. Does not block.

        `nickname=None` addresses every other member of the channel. Sent with NOBLOCK, so reaching SNDHWM raises
        `zmq.Again` rather than waiting; blocking here would stall the MCP call and with it the user's session.
        """
        self._require_on_air()
        assert self.dealer is not None and self.channel is not None
        destination = protocol.dumps(protocol.destination(self.channel, nickname))
        try:
            await self.dealer.send_multipart([destination, body.encode("utf-8")], zmq.NOBLOCK)
        except zmq.Again:
            raise RuntimeError("send queue is full -- the hub is not keeping up") from None
        return protocol.new_ulid()

    def receive(self) -> list[dict[str, Any]]:
        """Return messages appended since the last call and advance the inbox cursor past them."""
        self._require_on_air()
        assert self.inbox is not None
        return self.inbox.read_new()

    def pending_count(self) -> int:
        """Number of unread messages, leaving the cursor where it is."""
        return self.inbox.pending_count() if self.inbox is not None else 0

    def peers(self) -> list[str]:
        """Nicknames on this channel other than this participant's, from the cached roster."""
        self._require_on_air()
        return [p for p in self.roster if p != self.nickname]

    async def disconnect(self) -> None:
        """Cancel the tasks, close the sockets, delete the inbox files, and return to the dormant state.

        Idempotent, because `connect` calls it on its own failure path and the caller may call it again afterwards.
        """
        was_on_air = self.on_air
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(BaseException):
                await task
        self._tasks.clear()

        for sock in (self.monitor, self.dealer, self.router):
            if sock is not None:
                sock.close()
        self.monitor = self.dealer = self.router = None
        self.hub = None

        if self.inbox is not None:
            self.inbox.destroy()
            self.inbox = None

        if was_on_air:
            log(f"off air ({self.nickname!r} on {self.channel!r})")
        self.handle = self.channel = self.nickname = None
        self.roster = []

    def close(self) -> None:
        """Terminate the ZMQ context. Call after `disconnect`, on process shutdown."""
        self.ctx.term()


def check_zmq_capabilities() -> None:
    """Raise if the installed pyzmq lacks a socket option this implementation requires.

    ROUTER_NOTIFY is deliberately not checked: it is a draft option absent from every released wheel, and nothing
    here uses it. See the `hub` module docstring and `Backend._monitor_loop` for the mechanisms used instead.
    """
    if missing := [n for n in ("ROUTER_MANDATORY", "ROUTER_HANDOVER") if not hasattr(zmq, n)]:
        raise RuntimeError(
            f"this pyzmq lacks {', '.join(missing)}; libzmq 4.2+ is required (found libzmq {zmq.zmq_version()})"
        )
