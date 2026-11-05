"""Waking a Codex session that is sitting idle.

A hook only fires when a session does something, so it cannot reach one waiting at its prompt. Codex can be
reached there, though not by delivering: `turn/start` on its app-server begins a turn, which is the programmatic
equivalent of the user typing. Everything else follows -- hooks fire, and the model reads its history.

So this is an alarm clock rather than a postman. It says that mail is waiting and lets `check_inbox` do the
reading, which is the division the notice socket already makes, for the same reason: what a session receives
should be what it chose to collect.

**It waits for an idle thread before knocking with `thread/queue/add`, and takes the knock back if the mail is
read first.** A queued prompt survives even if a hook reads the mail before the busy turn ends, so the line waits
for the `thread/status/changed` that says the thread went idle, and checks that mail still waits immediately
before knocking. Status and queue acceptance are separate operations with no atomic idle-and-enqueue between
them, so a prompt can still land behind a turn that has just begun; `withdraw` deletes it with
`thread/queue/delete` once the mail it announced has been read. Measured on codex-cli 0.151.0: queueing waited
behind a busy turn, while `turn/start` opened another turn alongside it. An older server without a queue gets
`turn/start`, only after idle, and that cannot be taken back.

**The door is a WebSocket the user opens on purpose, and the session finds it by looking up.**
`codex app-server --listen ws://127.0.0.1:4500` serves the thread protocol over a local socket, and that app-server
is this process's own forebear: measured on 2026-08-29, an MCP server started for a thread appears in the tree as
`app-server -> uv run ... yaac -> python ... yaac`. So the URL is read off the command line of the ancestor that
has it, which answers *which* app-server is running this session -- not merely which ones are listening on this
machine. A second app-server, or the permanent `codex app-server daemon` that carries no `--listen` at all, is
neither a candidate nor an ambiguity.

Nothing is configured and nothing is guessed: no variable, no daemon to find, no control socket, no relay, and no
subprocess of our own -- which also means no dependence on an event loop that can spawn one, and Windows runs the
selector loop here because pyzmq must. A session not running under an app-server has no such ancestor, finds
nothing, and is never woken.

It names no `model`, `cwd` or `approvalPolicy`: the call requires only the thread and the input, so Codex answers
those from the thread's own configuration rather than from our guess about it.

The same door keeps a joined session alive. A thread nobody is subscribed to is unloaded once idle long enough,
and this process with it, so a `Line` stays subscribed from the first join to the last leave -- and since it is
open anyway, the wake, the status it waits on, and the hooks check all go over it.
"""

import asyncio
import base64
import contextlib
import json
import logging
import os
import struct
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import processes

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15.0
HANDSHAKE_KEY = base64.b64encode(b"yaac------------").decode("ascii")
"""Any 16 bytes; the server hashes it back and nothing here checks the answer. A client that verified it would be
guarding against a server that cannot help it anyway."""


LISTEN = "--listen"
"""How the app-server is told which address to serve, and therefore where it is written down."""


def serving() -> str | None:
    """The app-server running this session, or None when nothing above us is one.

    Nearest forebear first, so a session under an app-server that is itself under another answers with the one
    that actually holds its thread.
    """
    lines = processes.command_lines()
    for pid in processes.ancestry():
        if (url := _listened(lines.get(pid, ""))) is not None:
            logger.info("running under an app-server at %s (pid %d)", url, pid)
            return url
    return None


def under_codex() -> bool:
    """Whether a Codex app-server is among this process's forebears, whatever it listens on.

    Codex 0.163 runs every plain `codex` session under one shared app-server it manages itself, listening on a
    Unix socket at an internal path (`codex app-server --listen unix:// --managed-daemon`). A session there is a
    Codex session `serving()` cannot reach, which is worth telling the model rather than leaving it to find out.
    """
    lines = processes.command_lines()
    return any(_is_codex_app_server(lines.get(pid, "")) for pid in processes.ancestry())


def _is_codex_app_server(command: str) -> bool:
    words = command.split()
    return bool(words) and Path(words[0].strip("\"'")).name.startswith("codex") and "app-server" in words[1:2]


def _listened(command: str) -> str | None:
    """The WebSocket address in `--listen ws://…`, written either as two words or joined by `=`.

    What follows the flag has to be a `ws://` or `wss://` address with a host in it, because `--listen` is a
    common flag and a process table is full of other people's programs: a bare port, a Unix socket path, or a
    quoted mention of the flag inside some other command's arguments are all things that would otherwise be
    knocked on. Quotes are stripped because a command line preserves whatever shell quoting reached it.
    """
    words = command.split()
    for index, word in enumerate(words):
        candidate = ""
        if word == LISTEN and index + 1 < len(words):
            candidate = words[index + 1]
        elif word.startswith(f"{LISTEN}="):
            candidate = word[len(LISTEN) + 1 :]
        candidate = candidate.strip("\"'")
        parsed = urlparse(candidate)
        if parsed.scheme in ("ws", "wss") and parsed.hostname:
            return candidate
    return None


def _frame(data: str | bytes, opcode: int = 0x1) -> bytes:
    """One masked frame, text unless told otherwise. Clients must mask; servers must not, which is the only
    asymmetry in the framing."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    first = 0x80 | opcode
    if len(payload) < 126:
        header = bytes([first, 0x80 | len(payload)])
    elif len(payload) <= 0xFFFF:
        header = bytes([first, 0xFE]) + struct.pack(">H", len(payload))
    else:
        header = bytes([first, 0xFF]) + struct.pack(">Q", len(payload))
    return header + mask + masked


async def _read_message(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
    """The next text message from the server, or "" once it closes.

    A connection that lives for a turn could read one frame and stop; a held one lives for days, so it answers
    pings, reassembles a message the server split across frames, and takes a close frame for the end it is.
    """
    parts: list[bytes] = []
    while True:
        head = await reader.readexactly(2)
        final, opcode, length = head[0] & 0x80, head[0] & 0x0F, head[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", await reader.readexactly(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", await reader.readexactly(8))[0]
        payload = await reader.readexactly(length) if length else b""
        match opcode:
            case 0x8:
                return ""
            case 0x9:
                writer.write(_frame(payload, opcode=0xA))
                continue
            case 0x0 | 0x1:
                parts.append(payload)
            case _:
                continue
        if final:
            return b"".join(parts).decode("utf-8", errors="replace")


async def _answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, request_id: int) -> dict[str, Any] | None:
    """The reply to one request, read past whatever else arrives first.

    The app-server talks while it works -- `thread/started`, `remoteControl/status/changed`, tool progress -- and
    a reader that took the next frame as its answer would pick up a notification instead. Matching on the id is
    the whole of the bookkeeping.
    """
    while True:
        raw = await _read_message(reader, writer)
        if not raw:
            return None
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("id") == request_id:
            return message


async def _request(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, message: dict[str, Any], timeout: float
) -> dict[str, Any] | None:
    writer.write(_frame(json.dumps(message)))
    await asyncio.wait_for(writer.drain(), timeout=timeout)
    return await asyncio.wait_for(_answer(reader, writer, message["id"]), timeout=timeout)


async def _open(endpoint: str, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
    """A WebSocket to the app-server at `endpoint`, past `initialize`. None, logged, when it would not have us.

    Raises OSError, TimeoutError or IncompleteReadError for a connection that fails partway; the caller owns the
    writer only once this returns it.
    """
    parsed = urlparse(endpoint)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    try:
        writer.write(
            f"GET {parsed.path or '/'} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {HANDSHAKE_KEY}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        handshake = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=timeout)
        if b"101" not in handshake.split(b"\r\n")[0]:
            logger.info("%s did not accept a websocket: %s", endpoint, handshake.split(b"\r\n")[0])
        else:
            # The app-server wants to know who it is talking to before it will do anything.
            identified = await _request(reader, writer, _initialize(), timeout)
            if identified is not None and "result" in identified:
                return reader, writer
    except BaseException:
        await _close(writer, timeout)
        raise
    await _close(writer, timeout)
    return None


async def _close(writer: asyncio.StreamWriter, timeout: float) -> None:
    writer.close()
    with contextlib.suppress(OSError, TimeoutError):
        await asyncio.wait_for(writer.wait_closed(), timeout=timeout)


def _initialize() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "clientInfo": {"name": "yaac", "title": "YAAC", "version": "0"},
            "capabilities": {"experimentalApi": True},
        },
    }


def _queue(thread: str, text: str) -> dict[str, Any]:
    """Put the alarm at the back of the thread's queue. Drains at once when the thread is idle, waits when it is
    not. `clientUserMessageId` is echoed back and read by nobody here; the server refuses the call without it."""
    return {"threadId": thread, "input": [{"type": "text", "text": text}], "clientUserMessageId": str(uuid.uuid4())}


def _turn(thread: str, text: str) -> dict[str, Any]:
    """The older door, for a server that has no queue. It names no `model`, `cwd` or `approvalPolicy`, since
    `turn/start` requires neither -- Codex answers those from the thread's own configuration."""
    return {"threadId": thread, "input": [{"type": "text", "text": text}]}


HOLD_RETRY_SECONDS = 1.0
MAX_HOLD_RETRY_SECONDS = 60.0

YAAC_HOOK_EVENTS = ("preToolUse", "stop")
"""The events the README's Codex hooks file answers, as `hooks/list` spells them."""


class Line:
    """One WebSocket to the app-server running this session, held from the first join to the last leave.

    Holding it is what keeps the session on the air. Codex shuts a loaded thread down once it has had no subscribed
    connection and no activity for `thread_unload_delay_secs` -- 1800 s unless configured -- and its MCP servers go
    with it. `thread/resume` subscribes the connection that sends it and a closed connection unsubscribes, so `run`
    resumes the thread and then reads for as long as it is not cancelled: every event of the thread arrives here,
    and a reader that stopped would leave the app-server writing into a full buffer.

    The same stream carries `thread/status/changed`, which is what `wake` waits on rather than asking. A dropped
    connection is reopened, backing off to a minute; a refused resume ends the line, since asking again gets the
    same answer. The app-server restarting is not a case to handle -- this process is its child and goes too.
    """

    def __init__(self, thread: str, url: str, timeout: float = TIMEOUT_SECONDS) -> None:
        self.thread, self.url, self.timeout = thread, url, timeout
        self.status: str | None = None
        self.refused = False
        self.ready = asyncio.Event()
        self._changed = asyncio.Condition()
        self._writer: asyncio.StreamWriter | None = None
        self._replies: dict[int, asyncio.Future] = {}
        self._next_id = 2
        self._queued: str | None = None

    async def run(self) -> None:
        retry = HOLD_RETRY_SECONDS
        while True:
            try:
                if (opened := await _open(self.url, self.timeout)) is not None:
                    reader, self._writer = opened
                    pump = asyncio.create_task(self._pump(reader, self._writer))
                    try:
                        match await self.request("thread/resume", {"threadId": self.thread, "excludeTurns": True}):
                            case {"result": result}:
                                logger.info("holding %s loaded through %s", self.thread, self.url)
                                retry = HOLD_RETRY_SECONDS
                                await self._settle(await self._status_after(result))
                                self.ready.set()
                                await pump
                            case {"error": error}:
                                logger.info("the app-server would not hold %s: %s", self.thread, error)
                                self.refused = True
                                await self._settle(None)
                                return
                    finally:
                        self.ready.clear()
                        pump.cancel()
                        writer, self._writer = self._writer, None
                        await _close(writer, self.timeout)
                        await self._settle(None)
            except (OSError, TimeoutError, ConnectionError, asyncio.IncompleteReadError) as exc:
                logger.info("lost the hold on %s through %s: %s", self.thread, self.url, exc)
            await asyncio.sleep(retry)
            retry = min(retry * 2, MAX_HOLD_RETRY_SECONDS)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one request on the held connection and return the reply. Raises ConnectionError when not connected."""
        if (writer := self._writer) is None:
            raise ConnectionError(f"no connection to {self.url}")
        request_id, self._next_id = self._next_id, self._next_id + 1
        reply = self._replies[request_id] = asyncio.get_running_loop().create_future()
        try:
            writer.write(_frame(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})))
            await asyncio.wait_for(writer.drain(), timeout=self.timeout)
            return await asyncio.wait_for(reply, timeout=self.timeout)
        except asyncio.CancelledError:
            # The pump cancels every reply still owed when the connection ends; only the caller's own
            # cancellation is a cancellation.
            if reply.cancelled() and not asyncio.current_task().cancelling():
                raise ConnectionError(f"lost the connection to {self.url}") from None
            raise
        finally:
            self._replies.pop(request_id, None)

    async def wake(self, text: str, unread: Callable[[], bool] = lambda: True) -> bool:
        """Put `text` in front of the thread as if the user had typed it, once it is idle. True when accepted.

        Waits, without a deadline, for the connection and for the thread to stop working -- cancel the task when the
        inbox drains. The status is the one the app-server last announced, and it lags the start of a turn, so a
        prompt can still land behind a turn that has just begun; `withdraw` takes it back if the mail is read first.
        Every other failure is quiet and false: the mail is in the inbox either way.
        """
        async with self._changed:
            await self._changed.wait_for(lambda: not unread() or self.refused or self.status not in (None, "active"))
        if not unread() or self.status != "idle":
            return False
        try:
            knocked = await self.request("thread/queue/add", _queue(self.thread, text))
            # An older app-server does not know the queue at all. Its answer names the method it could not parse,
            # which is the only thing separating "too old" from "no such thread" -- both arrive as -32600.
            if unread() and "thread/queue/add" in str(knocked.get("error", "")):
                logger.info("%s is too old for the queue; starting a turn in %s instead", self.url, self.thread)
                knocked = await self.request("turn/start", _turn(self.thread, text))
        except (OSError, TimeoutError, ConnectionError) as exc:
            logger.info("could not wake %s through %s: %s", self.thread, self.url, exc)
            return False
        match knocked:
            case {"result": {"queuedSubmission": {"id": str(queued)}}}:
                self._queued = queued
                return True
            case {"result": _}:
                return True
            case {"error": error}:
                logger.info("the app-server refused to wake %s: %s", self.thread, error)
        return False

    async def withdraw(self) -> None:
        """Take back the prompt `wake` queued, if it has not run yet. The mail it announced has been read."""
        if (queued := self._queued) is None:
            return
        self._queued = None
        try:
            answer = await self.request("thread/queue/delete", {"threadId": self.thread, "queuedSubmissionId": queued})
        except (OSError, TimeoutError, ConnectionError) as exc:
            logger.info("could not withdraw the wake from %s: %s", self.thread, exc)
        else:
            logger.info("withdrew the wake from %s: %s", self.thread, answer.get("result", answer.get("error")))

    async def hook_trouble(self, wait: float) -> str | None:
        """What is wrong with this thread's YAAC hooks, or None when they will run -- or when it cannot be told.

        Codex stops running a hook whose file changed since the user approved it, never runs a project's hooks
        until the project is trusted, and says neither to the model; a session then misses mail mid-turn with no
        sign why. `hooks/list` reports per thread, so it sees the thread's own working directory and config.
        """
        try:
            await asyncio.wait_for(self.ready.wait(), timeout=wait)
            answer = await self.request("hooks/list", {"threadId": self.thread})
        except (OSError, TimeoutError, ConnectionError) as exc:
            logger.info("could not list the hooks of %s: %s", self.thread, exc)
            return None
        match answer:
            case {"result": {"data": list(entries)}}:
                return _hook_trouble(entries)
        return None

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._dispatch(reader, writer)
        finally:
            for reply in self._replies.values():
                reply.cancel()

    async def _dispatch(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while raw := await _read_message(reader, writer):
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            match message:
                case {"id": int(request_id)} if (reply := self._replies.get(request_id)) and not reply.done():
                    reply.set_result(message)
                case {"method": "thread/status/changed", "params": {"threadId": self.thread, "status": status}}:
                    await self._settle(_kind(status))

    async def _status_after(self, resumed: Any) -> str:
        """The thread's status on (re)connecting. `thread/resume` carries it on 0.166; a server whose answer does
        not is asked with `thread/read`, and one that answers neither is a thread this line will not wake."""
        if (status := _status_of(resumed)) is None:
            read = await self.request("thread/read", {"threadId": self.thread, "includeTurns": False})
            status = _status_of(read.get("result"))
        return status or "unknown"

    async def _settle(self, status: str | None) -> None:
        async with self._changed:
            self.status = status
            self._changed.notify_all()


def _status_of(result: Any) -> str | None:
    match result:
        case {"thread": {"status": status}}:
            return _kind(status)
    return None


def _kind(status: Any) -> str | None:
    match status:
        case {"type": str(kind)}:
            return kind
    return None


def _is_yaac_hook(hook: dict[str, Any]) -> bool:
    return "yaac-hook" in str(hook.get("command", "")) or hook.get("tool") == "hook_report"


def _hook_trouble(entries: list[Any]) -> str | None:
    hooks = [h for e in entries if isinstance(e, dict) for h in e.get("hooks", []) if isinstance(h, dict)]
    errors = [
        f"{error.get('path')}: {error.get('message')}"
        for e in entries
        if isinstance(e, dict)
        for error in e.get("errors", [])
        if isinstance(error, dict)
    ]
    found = []
    for event in YAAC_HOOK_EVENTS:
        mine = [h for h in hooks if h.get("eventName") == event and _is_yaac_hook(h)]
        match mine:
            case []:
                found.append(f"no {event} hook")
            case _ if any(h.get("enabled") and h.get("trustStatus") in ("trusted", "managed") for h in mine):
                pass
            case [first, *_] if not first.get("enabled"):
                found.append(f"the {event} hook is disabled")
            case [first, *_] if first.get("trustStatus") == "modified":
                found.append(f"the {event} hook changed since it was approved")
            case _:
                found.append(f"the {event} hook is not approved")
    if not found:
        return None
    if errors:
        found.append("hooks file errors: " + "; ".join(errors))
    return "; ".join(found)
