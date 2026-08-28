"""The notice socket: how a process outside this one learns that mail arrived.

The inbox lives in the server process's memory, so nothing outside it can see an arrival. Two readers need to, and
neither can call an MCP tool: Claude Code's `Monitor`, which turns each line it reads into an event injected into
a session that may be sitting idle, and a Codex hook, which is a separate program run between turns.

Both are children of the same client session that spawned this server, and a client hands its session id to all of
its children -- `CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID` -- so all three can derive the same port from it. That
is what keeps this file-free: no path to agree on, no port to write down, no registry to keep true.

Notices carry no body. They say that something arrived and for which membership; `check_inbox` still delivers.
So a notice that is dropped, capped, or never read costs nothing at all -- the mail is untouched in the inbox --
and no reader's size limit can turn a large message into a broken one.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import socket
from collections.abc import Callable
from typing import Any

SESSION_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
"""Where each client puts the id it gives every child process. Measured, not guessed: a Codex hook reads the same
value on stdin as `session_id`, and Claude Code's is already recorded as equal to a hook's `session_id`."""

# Below the ephemeral range on every platform (Linux starts at 32768, macOS and Windows higher still), so the
# kernel will not hand one of these out as a source port and collide with a listener that is about to exist.
PORT_BASE = 20_000
PORT_SPAN = 4_000
PORT_PROBES = 8
"""How far to walk when the derived port is taken. Two sessions can derive the same port, so both sides walk the
same short sequence and the reader confirms the session id in the answer rather than trusting the port alone."""

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455, the constant a server hashes the client key against
NOTICE_LIMIT = 200
"""Longest notice line. Names are user-chosen and unbounded, and a notice is a signal rather than content, so an
over-long one degrades to the part that is always true instead of being truncated into something misleading."""


def session_key() -> str | None:
    """The client session id this server was launched under, or None when the client offers none."""
    for variable in SESSION_ENV:
        if value := os.environ.get(variable):
            return value
    return None


def ports_for(key: str) -> list[int]:
    """The ports a given session's notice socket may be on, in the order both sides try them.

    blake2s rather than `hash()`, which is salted per process and would give the reader a different answer than
    the writer.
    """
    seed = int.from_bytes(hashlib.blake2s(key.encode("utf-8"), digest_size=4).digest(), "big")
    first = PORT_BASE + seed % PORT_SPAN
    return [PORT_BASE + (first - PORT_BASE + step) % PORT_SPAN for step in range(PORT_PROBES)]


def describe_arrival(channel: str, name: str, message: dict[str, Any]) -> str:
    """One line about one arrival, naming no body and no unbounded field it cannot afford."""
    match message:
        case {"kind": "bounce"}:
            kind = "a bounce"
        case {"kind": "error"}:
            kind = "a refusal"
        case {"to": to} if to:
            kind = "a whisper"
        case _:
            kind = "a broadcast"
    sender = (message.get("from") or {}).get("name") or "someone"
    line = f"1 new: {kind} on {channel!r} to you as {name!r}, from {sender!r} -- call check_inbox"
    return line if len(line) <= NOTICE_LIMIT else "1 new -- call check_inbox"


def _frame(text: str) -> bytes:
    """One unmasked WebSocket text frame. Servers never mask, and nothing here needs fragmenting."""
    payload = text.encode("utf-8")
    if len(payload) < 126:
        header = bytes([0x81, len(payload)])
    elif len(payload) <= 0xFFFF:
        header = bytes([0x81, 126]) + len(payload).to_bytes(2, "big")
    else:
        header = bytes([0x81, 127]) + len(payload).to_bytes(8, "big")
    return header + payload


def _accept(websocket_key: str) -> str:
    return base64.b64encode(hashlib.sha1((websocket_key + WS_GUID).encode("ascii")).digest()).decode("ascii")


class Notices:
    """A loopback listener serving one process's arrival notices, in the two shapes its readers can consume.

    A stream, for a watcher that stays connected: `Monitor` opens a WebSocket and every frame becomes an event in
    its session, which is the only way an idle session hears anything at all.

    A single answer, for a program that runs and exits: a Codex hook asks once per event and prints what it gets.

    One listener serves every membership this process holds, because they all belong to the session that owns the
    process. Each notice names its channel and name, so a session on several channels can tell them apart.
    """

    def __init__(self, key: str | None = None) -> None:
        self.key = key or session_key()
        self.port: int | None = None
        self.snapshot: Callable[[], list[dict[str, Any]]] = list
        self._server: asyncio.Server | None = None
        self._watchers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str | None:
        """What a watcher connects to, or None while nothing is listening."""
        return f"ws://127.0.0.1:{self.port}/{self.path}" if self.port else None

    @property
    def path(self) -> str:
        """The session id doubles as the path. It is not a secret, but it is not guessable either, so a scan of
        loopback finds a listener that answers nothing without it."""
        return self.key or "yaac"

    async def start(self) -> None:
        """Listen on the first free port of this session's sequence. Failure is not fatal: notices are an extra."""
        if self._server is not None:
            return
        candidates = ports_for(self.key) if self.key else [0]
        for port in candidates:
            try:
                self._server = await asyncio.start_server(self._serve, "127.0.0.1", port)
            except OSError:
                continue
            else:
                self.port = self._server.sockets[0].getsockname()[1]
                return

    async def stop(self) -> None:
        """Close the listener and every watcher, so the port is free the moment the last membership goes."""
        for writer in list(self._watchers):
            with contextlib.suppress(OSError):
                writer.close()
        self._watchers.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        self._server = None
        self.port = None

    def announce(self, line: str) -> None:
        """Send one notice to every watcher. A watcher that has gone is dropped rather than retried: it will
        reconnect or it will not, and the mail it missed is still in the inbox either way."""
        for writer in list(self._watchers):
            try:
                writer.write(_frame(line))
            except Exception:  # noqa: BLE001 -- a watcher is a guest; its failure is not the inbox's problem
                self._watchers.discard(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
        except (TimeoutError, asyncio.IncompleteReadError, OSError):
            writer.close()
            return

        head = request.decode("latin-1").split("\r\n")
        target = head[0].split(" ")[1] if " " in head[0] else "/"
        headers = {}
        for line in head[1:]:
            if ": " in line:
                field, _, value = line.partition(": ")
                headers[field.lower()] = value

        if target.lstrip("/") != self.path:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await self._drain_and_close(writer)
        elif "websocket" in headers.get("upgrade", "").lower():
            await self._stream(reader, writer, headers.get("sec-websocket-key", ""))
        else:
            await self._answer_once(writer)

    async def _answer_once(self, writer: asyncio.StreamWriter) -> None:
        """What a hook reads: the unread state now, and the session it belongs to so the reader can confirm it
        reached the right process rather than another session that derived the same port."""
        body = json.dumps({"session": self.key, "connections": self.snapshot()}).encode("utf-8")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        await self._drain_and_close(writer)

    async def _stream(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, websocket_key: str) -> None:
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            + f"Sec-WebSocket-Accept: {_accept(websocket_key)}\r\n\r\n".encode("ascii")
        )
        with contextlib.suppress(Exception):
            await writer.drain()
        self._watchers.add(writer)
        try:
            # Nothing a watcher sends is acted on; the read is how a closed connection is noticed.
            while await reader.read(1024):
                pass
        except OSError:
            pass
        finally:
            self._watchers.discard(writer)
            with contextlib.suppress(OSError):
                writer.close()

    @staticmethod
    async def _drain_and_close(writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await writer.drain()
        with contextlib.suppress(OSError):
            writer.close()


def ask(key: str, timeout: float = 1.0) -> dict[str, Any] | None:
    """Ask this session's notice socket what is waiting, from outside the process. Stdlib and blocking on purpose:
    the caller is a hook that runs between turns, and every import it does is latency the user waits through.

    Returns None when nothing is listening, which is the normal answer for a session that has joined nothing.
    """
    for port in ports_for(key):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout) as connection:
                connection.sendall(f"GET /{key} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
                received = bytearray()
                while chunk := connection.recv(4096):
                    received += chunk
        except OSError:
            continue
        _, _, body = bytes(received).partition(b"\r\n\r\n")
        try:
            answer = json.loads(body)
        except ValueError:
            continue
        # Another session may hold the first port of this sequence, so the answer has to name whose it is.
        if answer.get("session") == key:
            return answer
    return None
