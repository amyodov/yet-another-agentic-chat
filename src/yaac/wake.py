"""Waking a Codex session that is sitting idle.

A hook only fires when a session does something, so it cannot reach one waiting at its prompt. Codex can be
reached there, though not by delivering: `turn/start` on its app-server begins a turn, which is the programmatic
equivalent of the user typing. Everything else follows -- hooks fire, and the model reads its history.

So this is an alarm clock rather than a postman. It says that mail is waiting and lets `check_inbox` do the
reading, which is the division the notice socket already makes, for the same reason: what a session receives
should be what it chose to collect.

**It waits for an idle thread before knocking with `thread/queue/add`.** A queued prompt survives even if a
hook reads the mail before the busy turn ends. Read `thread/read` first, wait while active, and check that mail
still waits immediately before knocking. Status and queue acceptance are separate operations: a turn may start
between them, and Codex can briefly report idle at the start of a turn. This reduces stale prompts without an
atomic idle-and-enqueue operation. An older server without a queue gets `turn/start`, also only after idle.

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
POLL_SECONDS = 1.0
MAX_POLL_SECONDS = 10.0
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


def _frame(text: str) -> bytes:
    """One masked text frame. Clients must mask; servers must not, which is the only asymmetry in the framing."""
    payload = text.encode("utf-8")
    mask = os.urandom(4)
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    if len(payload) < 126:
        header = bytes([0x81, 0x80 | len(payload)])
    elif len(payload) <= 0xFFFF:
        header = bytes([0x81, 0xFE]) + struct.pack(">H", len(payload))
    else:
        header = bytes([0x81, 0xFF]) + struct.pack(">Q", len(payload))
    return header + mask + masked


async def _read_frame(reader: asyncio.StreamReader) -> str:
    """One text frame from the server. Unmasked, since that is what a server sends."""
    head = await reader.readexactly(2)
    length = head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", await reader.readexactly(8))[0]
    return (await reader.readexactly(length)).decode("utf-8", errors="replace") if length else ""


async def _answer(reader: asyncio.StreamReader, request_id: int) -> dict[str, Any] | None:
    """The reply to one request, read past whatever else arrives first.

    The app-server talks while it works -- `thread/started`, `remoteControl/status/changed`, tool progress -- and
    a reader that took the next frame as its answer would pick up a notification instead. Matching on the id is
    the whole of the bookkeeping.
    """
    while True:
        raw = await _read_frame(reader)
        if not raw:
            return None
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        if message.get("id") == request_id:
            return message


def _queue(request_id: int, thread: str, text: str) -> dict[str, Any]:
    """Put the alarm at the back of the thread's queue. Drains at once when the thread is idle, waits when it is
    not. `clientUserMessageId` is echoed back and read by nobody here; the server refuses the call without it."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "thread/queue/add",
        "params": {
            "threadId": thread,
            "input": [{"type": "text", "text": text}],
            "clientUserMessageId": str(uuid.uuid4()),
        },
    }


def _turn(request_id: int, thread: str, text: str) -> dict[str, Any]:
    """The older door, for a server that has no queue. It names no `model`, `cwd` or `approvalPolicy`, since
    `turn/start` requires neither -- Codex answers those from the thread's own configuration."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "turn/start",
        "params": {"threadId": thread, "input": [{"type": "text", "text": text}]},
    }


async def wake(
    thread: str,
    text: str,
    url: str | None = None,
    timeout: float = TIMEOUT_SECONDS,
    *,
    unread: Callable[[], bool] = lambda: True,
) -> bool:
    """Put `text` in front of `thread` as if the user had typed it. True when the app-server accepted it.

    Every failure is quiet and false: nothing listening on that URL, no such thread, a server that refuses. The
    mail is in the inbox either way, and a session that cannot be woken is exactly a session that reads its mail
    the next time it does something. Active threads wait with bounded I/O and polling that backs off to ten
    seconds; cancel the task when its inbox drains or the owning session closes.
    """
    if (endpoint := url or serving()) is None:
        return False
    parsed = urlparse(endpoint)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 80

    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        logger.info("nothing listening at %s to wake %s: %s", endpoint, thread, exc)
        return False

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
            return False

        # The app-server wants to know who it is talking to before it will do anything.
        writer.write(
            _frame(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {"name": "yaac", "title": "YAAC", "version": "0"},
                            "capabilities": {"experimentalApi": True},
                        },
                    }
                )
            )
        )
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        identified = await asyncio.wait_for(_answer(reader, 1), timeout=timeout)
        if identified is None or "error" in identified or "result" not in identified:
            return False

        interval = POLL_SECONDS
        request_id = 2
        while unread():
            writer.write(
                _frame(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": "thread/read",
                            "params": {"threadId": thread, "includeTurns": False},
                        }
                    )
                )
            )
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            status = await asyncio.wait_for(_answer(reader, request_id), timeout=timeout)
            request_id += 1
            match status:
                case {"error": _}:
                    return False
                case {"result": {"thread": {"status": {"type": str(kind)}}}}:
                    pass
                case _:
                    return False
            if kind == "idle":
                break
            if kind != "active":
                return False
            await asyncio.sleep(interval)
            interval = min(interval * 2, MAX_POLL_SECONDS)
        if not unread():
            return False

        writer.write(_frame(json.dumps(_queue(request_id, thread, text))))
        await asyncio.wait_for(writer.drain(), timeout=timeout)
        knocked = await asyncio.wait_for(_answer(reader, request_id), timeout=timeout)
        request_id += 1

        # An older app-server does not know the queue at all. Its answer names the method it could not parse,
        # which is the only thing separating "too old" from "no such thread" -- both arrive as -32600.
        if knocked is not None and unread() and "thread/queue/add" in str(knocked.get("error", "")):
            logger.info("%s is too old for the queue; starting a turn in %s instead", endpoint, thread)
            writer.write(_frame(json.dumps(_turn(request_id, thread, text))))
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            knocked = await asyncio.wait_for(_answer(reader, request_id), timeout=timeout)
    except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
        logger.info("could not wake %s through %s: %s", thread, endpoint, exc)
        return False
    finally:
        writer.close()
        with contextlib.suppress(OSError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), timeout=timeout)

    if knocked is None:
        return False
    if error := knocked.get("error"):
        logger.info("the app-server refused to wake %s: %s", thread, error)
        return False
    return "result" in knocked
