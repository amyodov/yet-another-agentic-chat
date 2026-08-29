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
                match message["method"]:
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
    "value,wanted",
    [("ws://127.0.0.1:4500", "ws://127.0.0.1:4500"), ("  ws://x:1  ", "ws://x:1"), ("", None), (None, None)],
    ids=["url", "padded", "empty", "unset"],
)
def test_waking_is_off_until_the_user_names_a_door(monkeypatch, value: str | None, wanted: str | None) -> None:
    """Starting a turn spends tokens and runs tools in somebody's session, so the opt-in is the address itself:
    there is nothing to discover, and an unset variable means this session is never woken."""
    monkeypatch.delenv(wake.WAKE_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(wake.WAKE_ENV, value)
    assert wake.wanted() == wanted


async def test_a_wake_joins_the_queue_of_the_named_thread(app_server: FakeAppServer) -> None:
    """The whole exchange, against a real socket: identify, then join the queue.

    `experimentalApi` is not decoration -- codex-cli 0.151.0 answers -32600 without it. Nothing here names a
    `model`, `cwd` or `approvalPolicy` either, since the call requires none of them and Codex answers them from
    the thread's own configuration rather than from our guess about it.
    """
    assert await wake.wake("01THREAD", "mail arrived", url=app_server.url) is True

    identify, queued = app_server.requests
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

    assert [request["method"] for request in old.requests] == ["initialize", "thread/queue/add", "turn/start"]
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
    monkeypatch.setenv(wake.WAKE_ENV, app_server.url)
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
    """Two independent conditions, because either alone would be a session woken by surprise."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"quiet-{endpoint.rsplit(':', 1)[1]}")

    listener, talker = Backend(endpoint), Backend(endpoint)
    try:
        await listener.connect(FORUM, "ann")
        await talker.connect(FORUM, "bob")

        monkeypatch.setenv(wake.WAKE_ENV, app_server.url)  # a door, but nothing knows how to name this session
        listener.notices.thread = None
        await talker.resolve(None).send("no thread", name="ann")
        await asyncio.sleep(SETTLE)

        monkeypatch.delenv(wake.WAKE_ENV, raising=False)  # reachable, but nobody asked
        listener.notices.thread = "01THREAD"
        await talker.resolve(None).send("no opt-in", name="ann")
        await asyncio.sleep(SETTLE)

        assert app_server.requests == []
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()
