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

from yaac import frontend, wake
from yaac.backend import Backend

FORUM = "forum"
SETTLE = 0.4
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class FakeAppServer:
    """The smallest thing that behaves like `codex app-server --listen ws://…`.

    It answers `initialize`, `thread/resume` with the thread's status, and the queue, and announces every status
    change with `thread/status/changed` to every connection, as the real one does -- assigning `status` is a turn
    starting or ending. It also talks over itself first: the real one emits `remoteControl/status/changed` and
    `thread/started` between a request and its reply, which is what makes matching on the id necessary.

    `queueless` is the app-server from before the queue existed, which answers exactly as codex-cli does: -32600
    naming the method it could not parse. Distinguishing that from -32600 for a thread it does not have is the
    whole of the fallback, so a fake that answered a tidier error would agree with a wrong implementation.
    """

    def __init__(self, chatty: bool = True, queueless: bool = False) -> None:
        self.requests: list[dict[str, Any]] = []
        self.chatty = chatty
        self.queueless = queueless
        self._status = "idle"
        self.resume_reports_status = True
        self.status_error = False
        self.resume_delay = 0.0
        self.queue_ack = asyncio.Event()
        self.queue_ack.set()
        self.queued: list[str] = []
        self.hooks: list[dict[str, Any]] | None = None
        self.refused: set[str] = set()
        self.pongs: list[bytes] = []
        self.connections = set()
        self.server: asyncio.Server | None = None

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, kind: str) -> None:
        self._status = kind
        for writer in self.connections:
            self._send(
                writer,
                {"method": "thread/status/changed", "params": {"threadId": "01THREAD", "status": {"type": kind}}},
            )

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            self.drop()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._exchange(reader, writer)
        finally:
            self.connections.discard(writer)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

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
        self.connections.add(writer)

        if self.chatty:
            self._send(writer, {"method": "remoteControl/status/changed", "params": {"status": "disabled"}})
        with contextlib.suppress(asyncio.IncompleteReadError, ConnectionError):
            while True:
                opcode, payload = await self._read(reader)
                if opcode == 0xA:
                    self.pongs.append(payload)
                    continue
                message = json.loads(payload)
                self.requests.append(message)
                if self.chatty:
                    self._send(writer, {"method": "thread/started", "params": {}})
                method, params = message["method"], message.get("params", {})
                if method == "thread/resume":
                    await asyncio.sleep(self.resume_delay)
                elif method == "thread/queue/add":
                    await self.queue_ack.wait()
                match method:
                    case _ if method in self.refused:
                        self._send(writer, {"id": message["id"], "error": {"code": -32600, "message": "refused"}})
                    case "thread/resume":
                        thread = {"id": "01THREAD"}
                        if self.resume_reports_status:
                            thread["status"] = {"type": self.status}
                        self._send(writer, {"id": message["id"], "result": {"thread": thread}})
                    case "thread/read" if self.status_error:
                        self._send(writer, {"id": message["id"], "error": {"code": -32600}})
                    case "thread/read":
                        status = {"thread": {"status": {"type": self.status}}}
                        self._send(writer, {"id": message["id"], "result": status})
                    case "thread/queue/add" if self.queueless:
                        error = {"code": -32600, "message": "Invalid request: unknown variant `thread/queue/add`"}
                        self._send(writer, {"id": message["id"], "error": error})
                    case "thread/queue/add":
                        queued = f"Q{len(self.queued) + 1}"
                        self.queued.append(queued)
                        submission = {"id": queued, "input": params["input"], "clientUserMessageId": "x"}
                        self._send(writer, {"id": message["id"], "result": {"queuedSubmission": submission}})
                    case "thread/queue/delete":
                        deleted = params["queuedSubmissionId"] in self.queued
                        if deleted:
                            self.queued.remove(params["queuedSubmissionId"])
                        self._send(writer, {"id": message["id"], "result": {"deleted": deleted}})
                    case "hooks/list" if self.hooks is not None:
                        data = [{"cwd": "/w", "hooks": self.hooks, "warnings": [], "errors": []}]
                        self._send(writer, {"id": message["id"], "result": {"data": data}})
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

    def ping(self, payload: bytes) -> None:
        for writer in self.connections:
            writer.write(bytes([0x89, len(payload)]) + payload)

    def drop(self) -> None:
        for writer in list(self.connections):
            writer.close()

    @staticmethod
    async def _read(reader: asyncio.StreamReader) -> tuple[int, bytes]:
        """A client's frame, which is masked -- unmasking it here is what proves the client masked it."""
        head = await reader.readexactly(2)
        assert head[1] & 0x80, "a client must mask every frame it sends"
        length = head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        mask = await reader.readexactly(4)
        payload = await reader.readexactly(length)
        return head[0] & 0x0F, bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))


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


async def until(condition):
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.005)


async def wait_for_requests(server, method, count=1):
    async with asyncio.timeout(2):
        while len([r for r in server.requests if r["method"] == method]) < count:
            await asyncio.sleep(0.005)


@pytest.fixture
async def waking_backend(endpoint, monkeypatch, app_server):
    monkeypatch.setattr(wake, "HOLD_RETRY_SECONDS", 0.01)
    listener = Backend(endpoint)
    monkeypatch.setattr(listener, "door", lambda: None)
    monkeypatch.setattr(listener, "serving", lambda: app_server.url)
    listener.notices.thread = "01THREAD"  # as join_channel sets it, before the first hello
    await listener.connect(FORUM, "ann")
    await until(lambda: listener._line.ready.is_set())
    yield listener
    await listener.disconnect_all()
    listener.close()
    await asyncio.sleep(0)


async def announce(backend, server, kind):
    """A turn starting or ending, as this session's line has heard it: the notification has arrived."""
    server.status = kind
    await until(lambda: backend._line.status == kind)


def arrive(backend, membership=None):
    membership = membership or backend.resolve(None)
    membership.inbox.append({"id": str(len(membership.inbox))})
    backend.rouse(membership)


def queues(server):
    return [r for r in server.requests if r["method"] == "thread/queue/add"]


async def test_busy_hook_drain_cancels_pending_wake(waking_backend, app_server):
    await announce(waking_backend, app_server, "active")
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/resume")
    waking_backend.receive_all()
    await asyncio.sleep(0.04)
    await announce(waking_backend, app_server, "idle")
    await asyncio.sleep(0.04)
    assert queues(app_server) == []
    assert waking_backend._waking is None


async def test_busy_to_idle_coalesces_all_arrivals(waking_backend, app_server):
    await announce(waking_backend, app_server, "active")
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/resume")
    arrive(waking_backend)
    arrive(waking_backend)
    await announce(waking_backend, app_server, "idle")
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
    await announce(waking_backend, app_server, "active")
    first.receive()
    await asyncio.sleep(0.04)
    assert len(queues(app_server)) == 1
    await announce(waking_backend, app_server, "idle")
    await wait_for_requests(app_server, "thread/queue/add", 2)
    assert second.pending_count() == 1


async def test_disconnect_cancels_busy_wait(waking_backend, app_server):
    await announce(waking_backend, app_server, "active")
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/resume")
    await waking_backend.disconnect_all()
    app_server.status = "idle"
    await asyncio.sleep(0.04)
    assert queues(app_server) == []
    assert waking_backend._waking is None


async def test_close_cancels_busy_wait(waking_backend, app_server):
    await announce(waking_backend, app_server, "active")
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/resume")
    pending = waking_backend._waking
    waking_backend.close()
    await asyncio.sleep(0.04)
    assert pending.cancelled()
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
    await announce(waking_backend, app_server, "active")
    arrive(waking_backend)
    replacement = waking_backend._waking
    app_server.queue_ack.set()
    await asyncio.sleep(0.04)
    assert waking_backend._waking is replacement
    assert len(queues(app_server)) == 1
    await announce(waking_backend, app_server, "idle")
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


@pytest.fixture
async def holding_backend(endpoint, monkeypatch, app_server):
    """A Codex session as `join_channel` leaves it: the thread is known before the first `hello`."""
    monkeypatch.setattr(wake, "HOLD_RETRY_SECONDS", 0.01)
    backend = Backend(endpoint)
    monkeypatch.setattr(backend, "door", lambda: None)
    monkeypatch.setattr(backend, "serving", lambda: app_server.url)
    backend.notices.thread = "01THREAD"
    yield backend
    await backend.disconnect_all()
    backend.close()
    await asyncio.sleep(0)


def resumes(server):
    return [r["params"] for r in server.requests if r["method"] == "thread/resume"]


@pytest.mark.parametrize("names", [["ann"], ["ann", "bob"]])
async def test_a_joined_session_holds_its_thread_until_it_leaves(holding_backend, app_server, names):
    for name in names:
        await holding_backend.connect(FORUM, name)
    await until(lambda: resumes(app_server))
    await asyncio.sleep(SETTLE)
    # One subscription for the whole process, however many memberships it holds, and it stays open.
    assert (resumes(app_server), len(app_server.connections)) == ([{"threadId": "01THREAD", "excludeTurns": True}], 1)

    await holding_backend.disconnect_all()
    await until(lambda: not app_server.connections)
    await holding_backend.connect(FORUM, "ann")
    await until(lambda: len(resumes(app_server)) == 2)


@pytest.mark.parametrize(
    ("frames", "expected"),
    [
        ([(0x81, b'{"a":1}')], '{"a":1}'),
        # Split across a text frame and a continuation, with a ping between them, which a server may interleave.
        ([(0x01, b'{"a":'), (0x89, b"p"), (0x80, b"1}")], '{"a":1}'),
        ([(0x88, b"")], ""),
    ],
)
async def test_a_message_is_read_whole_whatever_framing_carries_it(frames, expected):
    reader = asyncio.StreamReader()
    for first, payload in frames:
        reader.feed_data(bytes([first, len(payload)]) + payload)
    written = []

    class Writer:
        def write(self, data):
            written.append(data)

    assert await wake._read_message(reader, Writer()) == expected


@contextlib.asynccontextmanager
async def line_to(server, timeout=wake.TIMEOUT_SECONDS):
    """A line held the way `Backend` holds one, for as long as the block runs."""
    line = wake.Line("01THREAD", server.url if isinstance(server, FakeAppServer) else server, timeout=timeout)
    task = asyncio.create_task(line.run())
    try:
        yield line
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def methods(server):
    return [r["method"] for r in server.requests]


@pytest.mark.parametrize(
    ("queueless", "knocks"),
    [(False, ["thread/queue/add"]), (True, ["thread/queue/add", "turn/start"])],
    ids=["queue", "too-old-for-the-queue"],
)
async def test_a_wake_identifies_holds_and_knocks(queueless, knocks):
    """The whole exchange, against a real socket. `experimentalApi` is not decoration -- codex-cli 0.151.0
    answers -32600 without it -- and the knock names no `model`, `cwd` or `approvalPolicy`, which Codex answers
    from the thread's own configuration. Only a server too old for the queue is knocked at twice."""
    server = FakeAppServer(queueless=queueless)
    await server.start()
    try:
        async with line_to(server) as line:
            assert await line.wake("mail arrived") is True
    finally:
        await server.stop()

    identify, resume, *knocked = server.requests
    assert (identify["method"], identify["params"]["capabilities"]) == ("initialize", {"experimentalApi": True})
    assert (resume["method"], resume["params"]) == ("thread/resume", {"threadId": "01THREAD", "excludeTurns": True})
    assert [r["method"] for r in knocked] == knocks
    assert [r["params"]["input"] for r in knocked] == [[{"type": "text", "text": "mail arrived"}]] * len(knocks)
    # Echoed back and read by nobody here, but the server refuses the queue without it.
    assert all(r["params"]["clientUserMessageId"] for r in knocked if r["method"] == "thread/queue/add")


@pytest.mark.parametrize(
    ("setup", "expected_methods"),
    [
        ({"refused": {"thread/resume"}}, ["initialize", "thread/resume"]),
        ({"refused": {"thread/queue/add"}}, ["initialize", "thread/resume", "thread/queue/add"]),
        ({"status": "notLoaded"}, ["initialize", "thread/resume"]),
        ({"status": "systemError"}, ["initialize", "thread/resume"]),
        ({"resume_reports_status": False, "status_error": True}, ["initialize", "thread/resume", "thread/read"]),
    ],
    ids=["resume-refused", "queue-refused", "not-loaded", "system-error", "status-unknowable"],
)
async def test_a_wake_that_cannot_be_delivered_is_quiet(app_server, setup, expected_methods):
    """Every failure is a false, never an exception: the mail is in the inbox either way."""
    for name, value in setup.items():
        setattr(app_server, "_status" if name == "status" else name, value)
    async with line_to(app_server) as line:
        assert await asyncio.wait_for(line.wake("mail"), 1) is False
    assert methods(app_server) == expected_methods


async def test_a_resume_without_a_status_is_asked_for_one(app_server):
    """0.166 puts the status in `thread/resume`'s answer; a server that does not is asked with `thread/read`."""
    app_server.resume_reports_status = False
    async with line_to(app_server) as line:
        assert await asyncio.wait_for(line.wake("mail"), 1) is True
    assert methods(app_server) == ["initialize", "thread/resume", "thread/read", "thread/queue/add"]


@pytest.mark.parametrize("read_first", [False, True], ids=["still-unread", "read-while-busy"])
async def test_a_busy_thread_is_knocked_on_when_it_says_it_is_idle(app_server, read_first):
    """No polling: the line hears `thread/status/changed`, and the wake moves only when the thread goes idle --
    and then only if the mail is still unread."""
    app_server._status = "active"
    unread = True
    async with line_to(app_server) as line:
        pending = asyncio.create_task(line.wake("mail", unread=lambda: unread))
        await until(lambda: line.status == "active")
        await asyncio.sleep(SETTLE)
        assert "thread/queue/add" not in methods(app_server)
        unread = not read_first
        app_server.status = "idle"
        assert await asyncio.wait_for(pending, 1) is not read_first
    assert methods(app_server).count("thread/queue/add") == int(not read_first)
    assert "thread/read" not in methods(app_server)


async def test_a_wake_waits_out_a_dropped_connection(app_server, monkeypatch):
    """A dropped line is reopened, pings are answered meanwhile, and a wake that was waiting for idle carries on
    waiting on the new connection rather than failing."""
    monkeypatch.setattr(wake, "HOLD_RETRY_SECONDS", 0.01)
    app_server._status = "active"
    async with line_to(app_server) as line:
        pending = asyncio.create_task(line.wake("mail"))
        await until(lambda: line.status == "active")
        app_server.ping(b"are you there")
        await until(lambda: app_server.pongs)
        app_server.drop()
        await until(lambda: methods(app_server).count("thread/resume") == 2 and line.status == "active")
        app_server.status = "idle"
        assert await asyncio.wait_for(pending, 1) is True
    assert app_server.pongs == [b"are you there"]


@pytest.mark.parametrize(
    "url", ["ws://127.0.0.1:9", "ws://no-such-host.invalid:4500", "not a url at all"], ids=["closed", "unknown", "junk"]
)
async def test_a_door_that_is_not_there_is_not_an_error(url, monkeypatch):
    """Nothing listening is retried quietly; a wake meanwhile waits rather than raising."""
    monkeypatch.setattr(wake, "HOLD_RETRY_SECONDS", 0.01)
    async with line_to(url, timeout=1) as line:
        pending = asyncio.create_task(line.wake("mail"))
        await asyncio.sleep(SETTLE)
        assert (pending.done(), line.ready.is_set()) == (False, False)
        pending.cancel()


async def test_mail_read_takes_back_the_prompt_it_queued(waking_backend, app_server):
    """The status lags a turn's start, so a prompt can land behind a turn that has just begun; if the hook then
    reads the mail during that turn, the prompt would wake the session to an empty inbox. It is withdrawn."""
    arrive(waking_backend)
    await wait_for_requests(app_server, "thread/queue/add")
    await until(lambda: waking_backend._wake_notified)
    assert app_server.queued == ["Q1"]
    waking_backend.receive_all()
    await wait_for_requests(app_server, "thread/queue/delete")
    deletes = [r["params"] for r in app_server.requests if r["method"] == "thread/queue/delete"]
    assert (deletes, app_server.queued) == ([{"threadId": "01THREAD", "queuedSubmissionId": "Q1"}], [])


def hook(event, enabled=True, trust="trusted", command="uvx --from yet-another-agentic-chat yaac-hook"):
    return {"eventName": event, "handlerType": "command", "command": command, "enabled": enabled, "trustStatus": trust}


@pytest.mark.parametrize(
    ("hooks", "trouble"),
    [
        ([hook("preToolUse"), hook("stop")], None),
        ([hook("preToolUse", trust="managed"), hook("stop")], None),
        (
            [
                hook("preToolUse"),
                {
                    "eventName": "stop",
                    "handlerType": "mcpTool",
                    "server": "yaac",
                    "tool": "hook_report",
                    "enabled": True,
                    "trustStatus": "trusted",
                },
            ],
            None,
        ),
        ([hook("preToolUse")], "no stop hook"),
        ([], "no preToolUse hook; no stop hook"),
        ([hook("preToolUse", command="echo hi"), hook("stop")], "no preToolUse hook"),
        ([hook("preToolUse", trust="modified"), hook("stop")], "the preToolUse hook changed since it was approved"),
        ([hook("preToolUse"), hook("stop", trust="untrusted")], "the stop hook is not approved"),
        ([hook("preToolUse"), hook("stop", enabled=False)], "the stop hook is disabled"),
    ],
    ids=["trusted", "managed", "mcp-tool", "no-stop", "none", "someone-elses", "modified", "untrusted", "disabled"],
)
async def test_hooks_that_will_not_run_are_named(waking_backend, app_server, hooks, trouble):
    """What `hooks/list` reports, read for YAAC's two hooks only. Shapes measured on codex-cli 0.166.0."""
    app_server.hooks = hooks
    assert await waking_backend.hook_trouble() == trouble
    assert [r["params"] for r in app_server.requests if r["method"] == "hooks/list"] == [{"threadId": "01THREAD"}]


async def test_a_session_without_a_line_reports_no_hook_trouble(endpoint):
    """A Claude Code session, or a Codex one YAAC cannot reach, has nobody to ask, which is not a problem to report."""
    backend = Backend(endpoint)
    try:
        await backend.connect(FORUM, "ann")
        assert await backend.hook_trouble() is None
    finally:
        await backend.disconnect_all()
        backend.close()


async def test_join_says_when_the_hooks_will_not_run(waking_backend, app_server, monkeypatch):
    """The model is the one that has to tell the user, so the result names the problem and both places it is
    fixed: `/hooks` in the terminal, and the approval dialog of the desktop app, which has no `/hooks`."""
    monkeypatch.setattr(frontend, "_radio", waking_backend)
    app_server.hooks = [hook("preToolUse")]
    joined = await frontend.join_channel("other", "carol")
    diagnosed = await frontend.dev_connections()
    assert joined["hooks"] == diagnosed["hooks"] == frontend._hooks_warning("no stop hook")
    assert "`/hooks`" in joined["hooks"] and "Codex app" in joined["hooks"]

    app_server.hooks = [hook("preToolUse"), hook("stop")]
    assert "hooks" not in await frontend.dev_connections()
