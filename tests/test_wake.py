"""Waking a Codex session that is sitting idle, which a hook cannot do.

A hook fires when a session acts; this fires when mail arrives. It is an alarm clock rather than a postman --
`thread/queue/add` puts a line in front of the session and `check_inbox` still does the reading.

The door is a WebSocket the user opens on purpose: `codex app-server --listen ws://127.0.0.1:4500`. So these run
against a real socket speaking the app-server's shape, rather than against a mock of it -- the framing, the
masking a client owes a server, and reading past the notifications it interleaves are exactly the parts that
would be wrong, and a mock would agree with whatever they did.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import struct
from collections.abc import AsyncIterator
from typing import Any

import pytest

from yaac import wake
from yaac.backend import Backend

FORUM = "forum"
SETTLE = 0.4
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeAppServer:
    """The smallest thing that behaves like `codex app-server --listen ws://…`.

    It answers `initialize`, answers `thread/queue/add`, and -- deliberately -- talks over itself first: the real
    one emits `remoteControl/status/changed` and `thread/started` between a request and its reply, which is what
    makes matching on the id necessary rather than tidy.

    `queueless` is the app-server from before the queue existed, which answers exactly as codex-cli does: -32600
    naming the method it could not parse. Distinguishing that from -32600 for a thread it does not have is the
    whole of the fallback, so a fake that answered a tidier error would agree with a wrong implementation.
    """

    def __init__(self, refuse: bool = False, chatty: bool = True, queueless: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.refuse = refuse
        self.chatty = chatty
        self.queueless = queueless
        self.status = "idle"
        self.status_error = False
        self.status_delay = 0.0
        self.queue_ack = asyncio.Event()
        self.queue_ack.set()
        self.connections = set()
        self.server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections.add(writer)
        try:
            await self._exchange(reader, writer)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.connections.discard(writer)

    async def _exchange(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = await reader.readuntil(b"\r\n\r\n")
        key = ""
        for line in request.decode("latin-1").split("\r\n"):
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            + f"Sec-WebSocket-Accept: {accept}\r\n\r\n".encode()
        )
        await writer.drain()

        if self.chatty:
            self._send(writer, {"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                message = json.loads(await self._read(reader))
                self.requests.append(message)
                if self.chatty:
                    self._send(writer, {"method": "thread/started", "params": {}})
                if message["method"] == "thread/read":
                    await asyncio.sleep(self.status_delay)
                elif message["method"] == "thread/queue/add":
                    await self.queue_ack.wait()
                match message["method"]:
                    case "thread/read" if self.status_error:
                        self._send(writer, {"id": message["id"], "error": {"code": -32600}})
                    case "thread/read":
                        self._send(
                            writer, {"id": message["id"], "result": {"thread": {"status": {"type": self.status}}}}
                        )
                    case "thread/queue/add" if self.queueless:
                        error = {"code": -32600, "message": "Invalid request: unknown variant `thread/queue/add`"}
                        self._send(writer, {"id": message["id"], "error": error})
                    case _ if self.refuse:
                        error = {"code": -32602, "message": "no such thread"}
                        self._send(writer, {"id": message["id"], "error": error})
                    case _:
                        self._send(writer, {"id": message["id"], "result": {"turn": {"status": "inProgress"}}})
                await writer.drain()

    @staticmethod
    def _send(writer: asyncio.StreamWriter, message: dict[str, Any]) -> None:
        payload = json.dumps(message).encode()
        if len(payload) < 126:
            header = bytes([0x81, len(payload)])
        else:
            header = bytes([0x81, 126]) + struct.pack(">H", len(payload))
        writer.write(header + payload)

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> str:
        """A client's frame, which is masked -- unmasking it here is what proves the client masked it."""
        head = await reader.readexactly(2)
        assert head[1] & 0x80, "a client must mask every frame it sends"
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        mask = await reader.readexactly(4)
        payload = await reader.readexactly(length)
        return bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload)).decode()


@pytest.fixture
async def app_server() -> AsyncIterator[FakeAppServer]:
    server = FakeAppServer()
    await server.start()
    yield server
    await server.stop()


@pytest.mark.parametrize(
    "command,found",
    [
        ("codex app-server --listen ws://127.0.0.1:4500", "ws://127.0.0.1:4500"),
        ("codex app-server --listen=ws://127.0.0.1:4500", "ws://127.0.0.1:4500"),
        ("codex app-server --listen wss://127.0.0.1:4500/thread", "wss://127.0.0.1:4500/thread"),
        ("codex app-server daemon pid-update-loop", None),
        ("some-other-server --listen 0.0.0.0:8080", None),
        ("some-other-server --listen unix:///tmp/s.sock", None),
        ("codex app-server --listen", None),
        ("", None),
    ],
    ids=["two-words", "joined", "wss-path", "daemon", "bare-port", "unix-socket", "nothing-after", "empty"],
)
def test_only_a_websocket_after_the_flag_counts(command: str, found: str | None) -> None:
    """`--listen` is a common flag and a process table is full of other people's programs. Codex's own permanent
    `app-server daemon` carries no address at all, and is the one every machine running Codex has -- so matching
    the program name rather than the address would knock on it every time."""
    assert wake._listened(command) == found


def test_the_app_server_found_is_the_one_this_session_runs_under(monkeypatch) -> None:
    """Measured on 2026-08-29: an MCP server started for a thread appears as `app-server -> uv run … yaac ->
    python … yaac`, so the answer is read off a forebear rather than searched for on the machine. Nearest first,
    because a session under an app-server that is itself under another belongs to the closer one -- and because
    a second app-server elsewhere on the machine is then not a candidate at all.
    """
    monkeypatch.setattr(wake.processes, "ancestry", lambda: [500, 400, 300, 200])
    monkeypatch.setattr(
        wake.processes,
        "command_lines",
        lambda: {
            500: "python .../yaac",
            400: "uv run --directory /repo yaac",
            300: "codex app-server --listen ws://127.0.0.1:4601",
            200: "codex app-server --listen ws://127.0.0.1:4500",
        },
    )
    assert wake.serving() == "ws://127.0.0.1:4601"


def test_a_session_under_no_app_server_is_never_woken(monkeypatch) -> None:
    """A Claude Code session, or a Codex one started without `--listen`, has no such forebear. Finding nothing is
    the ordinary answer and not a failure: the mail waits in the inbox exactly as it would have anyway."""
    monkeypatch.setattr(wake.processes, "ancestry", lambda: [500, 400])
    monkeypatch.setattr(wake.processes, "command_lines", lambda: {500: "python .../yaac", 400: "claude"})
    assert wake.serving() is None


async def test_a_wake_joins_the_queue_of_the_named_thread(app_server: FakeAppServer) -> None:
    """The whole exchange, against a real socket: identify, then join the queue.

    `experimentalApi` is not decoration -- codex-cli 0.151.0 answers -32600 without it. Nothing here names a
    `model`, `cwd` or `approvalPolicy` either, since the call requires none of them and Codex answers them from
    the thread's own configuration rather than from our guess about it.
    """
    assert await wake.wake("01THREAD", "mail arrived", url=app_server.url) is True

    identify, status, queued = app_server.requests
    assert status["method"] == "thread/read"
    assert status["params"] == {"threadId": "01THREAD", "includeTurns": False}
    assert identify["method"] == "initialize"
    assert identify["params"]["clientInfo"]["name"] == "yaac"
    assert identify["params"]["capabilities"] == {"experimentalApi": True}
    assert queued["method"] == "thread/queue/add"
    assert queued["params"]["threadId"] == "01THREAD"
    assert queued["params"]["input"] == [{"type": "text", "text": "mail arrived"}]
    # Echoed back and read by nobody here, but the server refuses the call without it.
    assert queued["params"]["clientUserMessageId"]


async def test_a_server_without_a_queue_gets_a_turn_instead() -> None:
    """Two doors to the same room, tried in the order of what they cost: the queue waits its place where
    `turn/start` opens a second turn beside the one already running. Only a server too old for the queue is
    knocked at twice."""
    old = FakeAppServer(queueless=True)
    await old.start()
    try:
        assert await wake.wake("01THREAD", "mail arrived", url=old.url) is True
    finally:
        await old.stop()

    assert [request["method"] for request in old.requests] == [
        "initialize",
        "thread/read",
        "thread/queue/add",
        "turn/start",
    ]
    assert old.requests[-1]["params"] == {"threadId": "01THREAD", "input": [{"type": "text", "text": "mail arrived"}]}


async def test_the_reply_is_found_past_whatever_the_server_says_first() -> None:
    """The real app-server narrates while it works, so the frame after a request is usually not its answer."""
    talkative = FakeAppServer(chatty=True)
    await talkative.start()
    try:
        assert await wake.wake("01THREAD", "mail arrived", url=talkative.url) is True
    finally:
        await talkative.stop()


async def test_a_refusal_is_quiet() -> None:
    """No such thread is the ordinary answer for a session that is not under an app-server at all."""
    refusing = FakeAppServer(refuse=True)
    await refusing.start()
    try:
        assert await wake.wake("01THREAD", "mail arrived", url=refusing.url) is False
    finally:
        await refusing.stop()


@pytest.mark.parametrize(
    "url", ["ws://127.0.0.1:9", "ws://no-such-host.invalid:4500", "not a url at all"], ids=["closed", "unknown", "junk"]
)
async def test_a_door_that_is_not_there_is_not_an_error(url: str) -> None:
    """Nothing listening is the normal case: `codex` may not be running, may not be installed, and this may be a
    Claude Code session that will never have an app-server at all. The mail waits in the inbox regardless."""
    assert await wake.wake("01THREAD", "mail arrived", url=url, timeout=2) is False


async def test_an_arrival_wakes_the_session_once(endpoint: str, monkeypatch, app_server: FakeAppServer) -> None:
    """One wake drains any number of messages, because reading is a pull -- so a second arrival while the first
    wake is still in flight needs no policy to coalesce it, and gets none."""
    monkeypatch.setattr(wake, "serving", lambda: app_server.url)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"wake-{endpoint.rsplit(':', 1)[1]}")

    listener, talker = Backend(endpoint), Backend(endpoint)
    try:
        await listener.connect(FORUM, "ann")
        listener.notices.thread = "01THREAD"  # what the hook would have told it
        await talker.connect(FORUM, "bob")
        await talker.resolve(None).send("first", name="ann")
        await talker.resolve(None).send("second", name="ann")
        await asyncio.sleep(SETTLE)

        turns = [r for r in app_server.requests if r.get("method") == "thread/queue/add"]
        assert len(turns) == 1
        text = turns[0]["params"]["input"][0]["text"]
        # It says who is speaking: an alarm that reads like the user talking is worse than no alarm.
        assert "check_inbox" in text and "Nobody typed this" in text
        assert FORUM in text
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()


async def test_nothing_is_woken_without_a_thread_or_an_opt_in(
    endpoint: str, monkeypatch, app_server: FakeAppServer
) -> None:
    """Two independent conditions, both discovered: an app-server above this process, and a thread id for it to
    start a turn in. Either alone is a session that cannot be woken correctly, so neither alone wakes one."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"quiet-{endpoint.rsplit(':', 1)[1]}")

    listener, talker = Backend(endpoint), Backend(endpoint)
    try:
        await listener.connect(FORUM, "ann")
        await talker.connect(FORUM, "bob")

        monkeypatch.setattr(wake, "serving", lambda: app_server.url)  # a door, but this session has no name
        listener.notices.thread = None
        await talker.resolve(None).send("no thread", name="ann")
        await asyncio.sleep(SETTLE)

        monkeypatch.setattr(wake, "serving", lambda: None)  # named, but nothing above it can start a turn
        listener.notices.thread = "01THREAD"
        await talker.resolve(None).send("no opt-in", name="ann")
        await asyncio.sleep(SETTLE)

        assert app_server.requests == []
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()


@pytest.mark.parametrize(
    "command,expected",
    [
        (
            "/opt/codex/0.163.0/bin/codex app-server --listen unix:// --managed-daemon",
            True,
        ),
        ("codex app-server --listen ws://127.0.0.1:4500", True),
        ("codex app-server daemon pid-update-loop", True),
        ("codex resume", False),
        ("/usr/bin/python app-server --listen unix://", False),
        ("", False),
    ],
    ids=["managed", "websocket", "daemon-loop", "tui", "not-codex", "empty"],
)
def test_a_codex_app_server_is_recognised_whatever_it_listens_on(monkeypatch, command: str, expected: bool) -> None:
    """The shared app-server Codex 0.163 runs every session under listens on a Unix socket, so `serving()` finds no
    door there; recognising it is what lets YAAC say so instead of staying silently unwakeable."""
    monkeypatch.setattr(wake.processes, "ancestry", lambda: [1, 2])
    monkeypatch.setattr(wake.processes, "command_lines", lambda: {1: "python yaac", 2: command})
    assert wake.under_codex() is expected


@pytest.mark.parametrize(
    "under_codex,serving,expected", [(True, None, True), (True, "ws://127.0.0.1:4500", False), (False, None, False)]
)
def test_a_session_is_stranded_only_under_codex_without_a_websocket(
    monkeypatch, under_codex, serving, expected
) -> None:
    monkeypatch.setattr(wake, "under_codex", lambda: under_codex)
    monkeypatch.setattr(wake, "serving", lambda: serving)
    backend = Backend("tcp://127.0.0.1:1")
    try:
        assert backend.stranded() is expected
    finally:
        backend.close()


async def test_busy_thread_waits_until_idle(app_server, monkeypatch):
    monkeypatch.setattr(wake, "POLL_SECONDS", 0.01, raising=False)
    app_server.status = "active"
    pending = asyncio.create_task(wake.wake("01THREAD", "mail", url=app_server.url))
    try:
        await asyncio.sleep(0.08)
        assert not any(r["method"] == "thread/queue/add" for r in app_server.requests)
        app_server.status = "idle"
        assert await asyncio.wait_for(pending, 1)
        assert len([r for r in app_server.requests if r["method"] == "thread/queue/add"]) == 1
    finally:
        pending.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pending


async def test_busy_mail_read_before_idle_never_queues(app_server, monkeypatch):
    monkeypatch.setattr(wake, "POLL_SECONDS", 0.01, raising=False)
    app_server.status = "active"
    unread = True
    pending = asyncio.create_task(wake.wake("01THREAD", "mail", url=app_server.url, unread=lambda: unread))
    await asyncio.sleep(0.05)
    unread = False
    app_server.status = "idle"
    assert await asyncio.wait_for(pending, 1) is False
    assert not any(r["method"] == "thread/queue/add" for r in app_server.requests)


@pytest.mark.parametrize("status", ["unknown", "notLoaded", "systemError"])
async def test_unknown_thread_status_never_queues(app_server, status):
    app_server.status = status
    assert await wake.wake("01THREAD", "mail", url=app_server.url) is False
    assert not any(r["method"] == "thread/queue/add" for r in app_server.requests)


async def test_status_api_error_never_queues(app_server):
    app_server.status_error = True
    assert await wake.wake("01THREAD", "mail", url=app_server.url) is False
    assert not any(r["method"] == "thread/queue/add" for r in app_server.requests)


async def test_busy_wait_cancellation_closes_connection(app_server, monkeypatch):
    monkeypatch.setattr(wake, "POLL_SECONDS", 0.01)
    app_server.status = "active"
    pending = asyncio.create_task(wake.wake("01THREAD", "mail", url=app_server.url))
    await asyncio.sleep(0.04)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not any(r["method"] == "thread/queue/add" for r in app_server.requests)


async def wait_for_requests(server, method, count=1):
    async with asyncio.timeout(2):
        while len([r for r in server.requests if r["method"] == method]) < count:
            await asyncio.sleep(0.005)


@pytest.fixture
async def waking_backend(endpoint, monkeypatch, app_server):
    monkeypatch.setattr(wake, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(wake, "MAX_POLL_SECONDS", 0.02)
    listener = Backend(endpoint)
    monkeypatch.setattr(listener, "door", lambda: None)
    monkeypatch.setattr(listener, "serving", lambda: app_server.url)
    await listener.connect(FORUM, "ann")
    listener.notices.thread = "01THREAD"
    yield listener
    await listener.disconnect_all()
    listener.close()
    await asyncio.sleep(0)


def arrive(backend, membership=None):
    membership = membership or backend.resolve(None)
    membership.inbox.append({"id": str(len(membership.inbox))})
    backend.rouse(membership)


def queues(server):
    return [r for r in server.requests if r["method"] == "thread/queue/add"]


async def test_busy_hook_drain_cancels_pending_wake(waking_backend, app_server):
    app_server.status = "active"
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/read")
    waking_backend.receive_all()
    await asyncio.sleep(0.04)
    app_server.status = "idle"
    await asyncio.sleep(0.04)
    assert queues(app_server) == []
    assert waking_backend._waking is None
    assert not app_server.connections


async def test_busy_to_idle_coalesces_all_arrivals(waking_backend, app_server):
    app_server.status = "active"
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/read")
    arrive(waking_backend)
    arrive(waking_backend)
    app_server.status = "idle"
    await wait_for_requests(app_server, "thread/queue/add")
    await asyncio.sleep(0.04)
    assert len(queues(app_server)) == 1


async def test_arrivals_after_acceptance_wait_for_read(waking_backend, app_server):
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/queue/add")
    await asyncio.sleep(0.02)
    arrive(waking_backend)
    await asyncio.sleep(0.04)
    assert len(queues(app_server)) == 1
    waking_backend.receive_all()
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/queue/add", 2)
    assert len(queues(app_server)) == 2


async def test_partial_membership_read_rearms_remaining_mail(waking_backend, app_server):
    first = waking_backend.resolve(None)
    await waking_backend.connect("other", "carol")
    second = next(m for m in waking_backend.memberships.values() if m.channel == "other")
    arrive(waking_backend, first)
    await wait_for_requests(app_server, "thread/queue/add")
    await asyncio.sleep(0.02)
    arrive(waking_backend, second)
    app_server.status = "active"
    first.receive()
    await asyncio.sleep(0.04)
    assert len(queues(app_server)) == 1
    app_server.status = "idle"
    await wait_for_requests(app_server, "thread/queue/add", 2)
    assert second.pending_count() == 1


async def test_disconnect_cancels_busy_wait(waking_backend, app_server):
    app_server.status = "active"
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/read")
    await waking_backend.disconnect_all()
    app_server.status = "idle"
    await asyncio.sleep(0.04)
    assert queues(app_server) == []
    assert waking_backend._waking is None


async def test_close_cancels_busy_wait(waking_backend, app_server):
    app_server.status = "active"
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/read")
    pending = waking_backend._waking
    waking_backend.close()
    await asyncio.sleep(0.04)
    assert pending.cancelled()
    assert queues(app_server) == []


async def test_status_timeout_never_queues(app_server):
    app_server.status_delay = 0.05
    assert await wake.wake("01THREAD", "mail", url=app_server.url, timeout=0.01) is False
    await asyncio.sleep(0.08)
    assert not app_server.connections
    assert queues(app_server) == []


async def test_closed_connection_never_queues(app_server):
    app_server.status_delay = 0.05
    pending = asyncio.create_task(wake.wake("01THREAD", "mail", url=app_server.url))
    await wait_for_requests(app_server, "thread/read")
    for writer in tuple(app_server.connections):
        writer.close()
    assert await asyncio.wait_for(pending, 1) is False
    assert queues(app_server) == []


async def test_delayed_acceptance_coalesces_arrivals(waking_backend, app_server):
    app_server.queue_ack.clear()
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/queue/add")
    arrive(waking_backend)
    arrive(waking_backend)
    app_server.queue_ack.set()
    await asyncio.sleep(0.04)
    assert len(queues(app_server)) == 1


async def test_read_during_acceptance_preserves_replacement_task(waking_backend, app_server):
    app_server.queue_ack.clear()
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/queue/add")
    waking_backend.receive_all()
    app_server.status = "active"
    arrive(waking_backend)
    replacement = waking_backend._waking
    app_server.queue_ack.set()
    await asyncio.sleep(0.04)
    assert waking_backend._waking is replacement
    assert len(queues(app_server)) == 1
    app_server.status = "idle"
    await wait_for_requests(app_server, "thread/queue/add", 2)


async def test_empty_read_and_disconnect_leave_accepted_wake_coalesced(waking_backend, app_server):
    first = waking_backend.resolve(None)
    await waking_backend.connect("other", "carol")
    second = next(m for m in waking_backend.memberships.values() if m.channel == "other")
    arrive(waking_backend, first)
    await wait_for_requests(app_server, "thread/queue/add")
    await asyncio.sleep(0.02)
    assert second.receive() == []
    await waking_backend.disconnect(second.routing_id)
    arrive(waking_backend, first)
    await asyncio.sleep(0.04)
    assert len(queues(app_server)) == 1
