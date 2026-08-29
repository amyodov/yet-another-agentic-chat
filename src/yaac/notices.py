"""The notice socket: how a process outside this one learns that mail arrived.

The inbox lives in the server process's memory, so nothing outside it can see an arrival. Two readers need to,
and neither can call an MCP tool: Claude Code's `Monitor`, which turns each line it reads into an event injected
into a session that may be sitting idle, and a Codex hook, which is a separate program run between turns.

**The address is published, not derived.** The socket takes whatever port the kernel offers, and the session
announces it in `hello`; anything that needs it asks the rendezvous point every participant already agrees on.
An earlier version computed the port from a name the user had to write into two config files, which was wrong
three times over: a client with one configuration block for the whole machine cannot give its sessions different
names, the digest is not reproducible from every language that might want to listen, and it made an address out
of something nobody had to agree on in the first place.

Notices carry no body. They say that something arrived and for which membership; `check_inbox` still delivers.
So a notice that is dropped, capped, or never read costs nothing at all -- the mail is untouched in the inbox --
and no reader's size limit can turn a large message into a broken one.
"""

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import socket
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from . import protocol
from .protocol import Envelope

logger = logging.getLogger(__name__)

CLIENT_ENV = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")
"""What a client calls the session it spawned, when it says so at all. Published in the directory so a reader can
recognise a session it already knows the name of; never used to compute anything, and its absence costs nothing."""

ASK_TIMEOUT_SECONDS = 0.5
"""One timeout for a question to a socket that may not be there. Loopback answers in microseconds when something
is listening, so this is generous for the case that works and cheap for the case that does not."""

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"  # RFC 6455, the constant a server hashes the client key against
NOTICE_LIMIT = 200
"""Longest notice line. Names are user-chosen and unbounded, and a notice is a signal rather than content, so an
over-long one degrades to the part that is always true instead of being truncated into something misleading."""


def client_session() -> str | None:
    """What this session's client calls it, if the client says. Claude Code does; Codex tells only its hooks."""
    for variable in CLIENT_ENV:
        if value := os.environ.get(variable):
            return value
    return None


def describe_arrival(channel: str, name: str, message: dict[str, Any]) -> str:
    """One line about one arrival, naming no body and no unbounded field it cannot afford."""
    envelope = Envelope.from_wire(message)
    if envelope.frm is not None and envelope.frm.is_the_hat:
        kind = {"bounce": "a bounce", "error": "a refusal"}.get(envelope.op or "", "operator mail")
    elif envelope.to.peer is not None:
        kind = "a whisper"
    else:
        kind = "a broadcast"
    sender = (envelope.frm.peer.name if envelope.frm and envelope.frm.peer else None) or "someone"
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

    def __init__(self, client: str | None = None) -> None:
        self.client = client or client_session()
        # A fresh token per process, so the address is unguessable even though the port is not chosen. It costs
        # nothing and keeps a scan of loopback from reading somebody's notices; it is not a secret, because
        # anything that can ask the rendezvous point is already being told it.
        self.token = protocol.new_ulid()
        self.port: int | None = None
        self.snapshot: Callable[[], list[dict[str, Any]]] = list
        # Which Codex thread this session is, learned from its hook rather than from the environment: Codex tells
        # a server it spawns nothing about the thread it serves, but it tells every hook, on stdin, every time.
        self.thread: str | None = None
        self._server: asyncio.Server | None = None
        self._watchers: set[asyncio.StreamWriter] = set()

    @property
    def url(self) -> str | None:
        """What a watcher connects to, or None while nothing is listening."""
        return f"ws://127.0.0.1:{self.port}/{self.path}" if self.port else None

    @property
    def path(self) -> str:
        """The token, which is what makes the published address specific rather than merely reachable."""
        return self.token

    async def start(self) -> None:
        """Listen on whatever port the kernel offers. Failure is not fatal: notices are an extra.

        Nothing is chosen and nothing is derived, so nothing can collide, be reserved by another program, or need
        reimplementing in another language. The address is published in `hello` and answered by the hat to anyone
        who asks -- which is the same one address everything else here already uses.
        """
        if self._server is not None:
            return
        try:
            self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        except OSError as exc:
            logger.warning("no notice socket this session: %s", exc)
            return
        self.port = self._server.sockets[0].getsockname()[1]

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
        except TimeoutError, asyncio.IncompleteReadError, OSError:
            writer.close()
            return

        head = request.decode("latin-1").split("\r\n")
        target = head[0].split(" ")[1] if " " in head[0] else "/"
        target, _, query = target.partition("?")
        if query.startswith("thread=") and (thread := query[len("thread=") :]):
            self.thread = thread
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
        body = json.dumps({"session": self.client, "connections": self.snapshot()}).encode("utf-8")
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


def ask(url: str, timeout: float = ASK_TIMEOUT_SECONDS, thread: str | None = None) -> dict[str, Any] | None:
    """Ask one session's notice socket what is waiting, given the address the directory published for it.

    Stdlib and blocking on purpose: the caller is a hook that runs between turns, and every import it makes is
    latency somebody waits through. One address, one connection, one timeout -- where an earlier version tried
    eight derived ports in turn and, on Windows, paid a full timeout for each of the seven that were not there.

    Returns None when nothing answers, which is the ordinary reply for a session that has since gone.
    """
    parsed = urlparse(url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 0
    where = f"{parsed.path or '/'}?thread={thread}" if thread else (parsed.path or "/")
    try:
        with socket.create_connection((host, port), timeout) as connection:
            connection.sendall(f"GET {where} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            received = bytearray()
            while chunk := connection.recv(4096):
                received += chunk
    except OSError:
        return None
    _, _, body = bytes(received).partition(b"\r\n\r\n")
    try:
        return json.loads(body)
    except ValueError:
        return None
