"""The notice socket, which is how anything outside the server process learns that mail arrived.

Two readers it has to satisfy, and they want different shapes of the same fact: a watcher that stays connected
and is told as things happen, and a program that runs once between turns and asks. Both find the port by deriving
it from the session id their client gave them, so what is worth pinning down is that the derivation agrees, that
a notice says enough to act on and no more than it can afford, and that the listener exists exactly as long as
there is a membership to talk about.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from yaac import notices, protocol
from yaac.backend import Backend
from yaac.hook import envelope
from yaac.notices import Notices, ask, describe_arrival, ports_for
from yaac.protocol import Address, Scope

FORUM = "forum"
SETTLE = 0.4

RadioFactory = Callable[[], Backend]


@pytest.fixture
async def radios(endpoint: str, monkeypatch) -> AsyncIterator[RadioFactory]:
    """Backends on their own net, under a session id no other test shares, so their notice sockets never collide."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"test-{endpoint.rsplit(':', 1)[1]}")
    made: list[Backend] = []

    def make() -> Backend:
        made.append(backend := Backend(endpoint))
        return backend

    yield make
    for backend in made:
        await backend.disconnect_all()
        backend.close()


async def watch(url: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a WebSocket the way Monitor would, and stop once the handshake is answered."""
    _, _, hostport_path = url.partition("//")
    hostport, _, path = hostport_path.partition("/")
    host, _, port = hostport.partition(":")
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(
        f"GET /{path} HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
    )
    await writer.drain()
    handshake = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    assert b"101 Switching Protocols" in handshake
    # RFC 6455's own example key and its answer, so the digest is checked against the standard rather than ourselves.
    assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in handshake
    return reader, writer


async def next_notice(reader: asyncio.StreamReader) -> str:
    """One text frame, unmasked as a server sends them."""
    header = await asyncio.wait_for(reader.readexactly(2), timeout=5)
    assert header[0] == 0x81  # FIN set, opcode 1: a whole text frame
    length = header[1]
    if length == 126:
        length = int.from_bytes(await reader.readexactly(2), "big")
    elif length == 127:
        length = int.from_bytes(await reader.readexactly(8), "big")
    return (await asyncio.wait_for(reader.readexactly(length), timeout=5)).decode("utf-8")


def test_both_sides_of_a_session_derive_the_same_ports() -> None:
    """The whole reason nothing is written to disk: the server and the hook are children of one client session and
    can each compute where the other is. `hash()` would not do -- it is salted per process."""
    assert ports_for("01a048c1-0c17-7fe2-b179-6c34aeaa5489") == ports_for("01a048c1-0c17-7fe2-b179-6c34aeaa5489")
    assert ports_for("one") != ports_for("another")
    walked = ports_for("01a048c1")
    for port in walked:
        # Below the ephemeral range on every platform, so the kernel cannot hand one out as a source port.
        assert 20_000 <= port < 24_000
    assert len(set(walked)) == len(walked)
    # Spread out on purpose: Windows reserves blocks of ports for Hyper-V and WSL, often a few hundred wide, and
    # a consecutive walk could land entirely inside one -- which looks like a feature that does not work there.
    assert min(abs(a - b) for a in walked for b in walked if a != b) > 100


def carried(**kwargs) -> dict[str, Any]:
    """A message as the hat would have handed it over, built by the real constructors so these cases cannot
    drift from the shape the wire actually carries."""
    return protocol.message(**kwargs).to_wire()


ALICE = Scope(peer=Address("ann"))


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            carried(to=Scope(channel=FORUM), frm=Scope(channel=FORUM, peer=Address("bob")), body="SECRET-BODY"),
            "1 new: a broadcast on 'forum' to you as 'ann', from 'bob' -- call check_inbox",
        ),
        (
            carried(
                to=Scope(channel=FORUM, peer=Address("ann")),
                frm=Scope(channel=FORUM, peer=Address("bob")),
                body="SECRET-BODY",
            ),
            "1 new: a whisper on 'forum' to you as 'ann', from 'bob' -- call check_inbox",
        ),
        (
            protocol.bounce("01J", "gone", to=ALICE).to_wire(),
            "1 new: a bounce on 'forum' to you as 'ann', from 'someone' -- call check_inbox",
        ),
        (
            protocol.error("refused", to=ALICE).to_wire(),
            "1 new: a refusal on 'forum' to you as 'ann', from 'someone' -- call check_inbox",
        ),
        (
            carried(to=Scope(channel=FORUM), frm=Scope(channel=FORUM, peer=Address("b" * 300)), body="SECRET-BODY"),
            "1 new -- call check_inbox",
        ),
    ],
    ids=["broadcast", "whisper", "bounce", "error", "unbounded-name"],
)
def test_a_notice_says_what_arrived_and_never_what_it_said(message: dict[str, Any], expected: str) -> None:
    """No body travels here, so no reader's size limit can turn a large message into a broken one, and a watcher
    that anyone on the machine could connect to learns nothing that was written. A name is user-chosen and
    unbounded, so an over-long one degrades to the part that stays true rather than being cut into a half-name."""
    assert describe_arrival(FORUM, "ann", message) == expected
    assert "SECRET-BODY" not in describe_arrival(FORUM, "ann", message)


async def test_an_arrival_reaches_a_watcher_that_is_doing_nothing(radios: RadioFactory) -> None:
    """The point of the whole file: a session with nothing running still gets told, because the watcher is a
    separate process holding a socket open rather than something the session has to remember to call."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    reader, writer = await watch(listener.notices.url)

    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("the field is recipient_group now", name="ann")

    assert await next_notice(reader) == "1 new: a whisper on 'forum' to you as 'ann', from 'bob' -- call check_inbox"
    # Told, not delivered: the message is exactly where check_inbox will find it.
    assert listener.resolve(None).pending_count() == 1
    writer.close()


async def test_a_program_that_runs_once_is_answered_with_what_is_waiting(radios: RadioFactory) -> None:
    """What a Codex hook does, since its hooks cannot call an MCP tool: ask, print, exit. The answer names the
    session because another session can hold the first port of this sequence."""
    listener = radios()
    joined = await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("waiting for you", name="ann")
    await asyncio.sleep(SETTLE)

    answer = await asyncio.to_thread(ask, listener.notices.key)
    assert answer["session"] == listener.notices.key
    assert [(c["channel"], c["name"], c["unread"]) for c in answer["connections"]] == [(FORUM, "ann", 1)]
    assert answer["connections"][0]["connection_id"] == joined.connection_id


async def test_nothing_listens_until_there_is_something_to_hear(radios: RadioFactory) -> None:
    """Hard rule 2 in the new place it can be broken: a dormant server opens no socket, and the port goes back the
    moment the last membership does."""
    quiet = radios()
    assert quiet.notices.url is None
    assert await asyncio.to_thread(ask, quiet.notices.key or "nobody") is None

    await quiet.connect(FORUM, "ann")
    assert quiet.notices.url is not None
    await quiet.disconnect_all()
    await asyncio.sleep(SETTLE)
    assert await asyncio.to_thread(ask, quiet.notices.key) is None


async def test_another_sessions_socket_answers_nothing(radios: RadioFactory) -> None:
    """The port is derived rather than announced, so a reader can reach the wrong process; the path is what makes
    that harmless. It is not a secret -- it is the session id -- but it is not guessable by a scan either."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    _, _, hostport_path = listener.notices.url.partition("//")
    hostport, _, _ = hostport_path.partition("/")
    host, _, port = hostport.partition(":")

    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(f"GET /someone-elses-session HTTP/1.1\r\nHost: {hostport}\r\nConnection: close\r\n\r\n".encode())
    await writer.drain()
    assert b"404" in await asyncio.wait_for(reader.read(64), timeout=5)
    writer.close()


@pytest.mark.parametrize(
    "event,client,field",
    [
        ("Stop", "claude-code", "hookSpecificOutput"),
        ("PreToolUse", "codex", "hookSpecificOutput"),
        ("Stop", "codex", "decision"),
        ("SubagentStop", "codex", "decision"),
    ],
)
def test_the_hook_program_and_the_hook_tool_speak_one_contract(event: str, client: str, field: str) -> None:
    """The out-of-process program and the in-process tool put text in front of a model the same way, so there is
    one place that knows what each client reads rather than two that can drift."""
    assert field in json.loads(envelope(event, client, "something arrived"))


def test_a_client_that_names_no_session_is_still_served(monkeypatch) -> None:
    """Claude Desktop offers no session id. The derived port is then unavailable -- so no out-of-process program
    can find it -- but a watcher told the url by join_channel still works, which is the reader that matters."""
    for variable in notices.SESSION_ENV:
        monkeypatch.delenv(variable, raising=False)
    assert Notices().key is None
    assert Notices().path == "yaac"
