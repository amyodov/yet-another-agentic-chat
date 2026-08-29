"""Waking a Codex session that is sitting idle.

A hook only fires when a session does something, so it cannot reach one waiting at its prompt. Codex can be
reached there, though not by delivering: `turn/start` on its app-server begins a turn, which is the programmatic
equivalent of the user typing. Everything else follows -- hooks fire, and the model reads its history.

So this is an alarm clock rather than a postman. It says that mail is waiting and lets `check_inbox` do the
reading, which is the division the notice socket already makes, for the same reason: what a session receives
should be what it chose to collect.

**It knocks with `thread/queue/add`, and only falls back to `turn/start`.** Measured against codex-cli 0.151.0:
on an idle thread the queue drains at once and a whole turn runs, so it wakes exactly as `turn/start` does; on a
thread that is already working it takes its place in line, where `turn/start` instead opens a second turn
alongside the first. Waking a session that is busy is the case this exists for, so the door that waits is the
right one. It costs two things -- `capabilities.experimentalApi` in `initialize`, without which the server
answers `-32600`, and a `clientUserMessageId`, which it echoes back and nothing here reads. A server too old for
it says `unknown variant 'thread/queue/add'`, and that is what the fallback is for.

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


async def wake(thread: str, text: str, url: str | None = None, timeout: float = TIMEOUT_SECONDS) -> bool:
    """Put `text` in front of `thread` as if the user had typed it. True when the app-server accepted it.

    Every failure is quiet and false: nothing listening on that URL, no such thread, a server that refuses. The
    mail is in the inbox either way, and a session that cannot be woken is exactly a session that reads its mail
    the next time it does something.
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
        await writer.drain()
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
        writer.write(_frame(json.dumps(_queue(2, thread, text))))
        await writer.drain()

        if await asyncio.wait_for(_answer(reader, 1), timeout=timeout) is None:
            logger.info("%s never finished the handshake; leaving %s asleep", endpoint, thread)
            return False
        knocked = await asyncio.wait_for(_answer(reader, 2), timeout=timeout)

        # An older app-server does not know the queue at all. Its answer names the method it could not parse,
        # which is the only thing separating "too old" from "no such thread" -- both arrive as -32600.
        if knocked is not None and "thread/queue/add" in str(knocked.get("error", "")):
            logger.info("%s is too old for the queue; starting a turn in %s instead", endpoint, thread)
            writer.write(_frame(json.dumps(_turn(3, thread, text))))
            await writer.drain()
            knocked = await asyncio.wait_for(_answer(reader, 3), timeout=timeout)
    except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
        logger.info("could not wake %s through %s: %s", thread, endpoint, exc)
        return False
    finally:
        with contextlib.suppress(OSError):
            writer.close()

    if knocked is None:
        return False
    if error := knocked.get("error"):
        logger.info("the app-server refused to wake %s: %s", thread, error)
        return False
    return "result" in knocked
