"""Delivering into a Claude Code session through the inbox socket it exports.

These run against a real Unix socket speaking the receiver's shape -- two NDJSON lines, auth then a user
message -- rather than a mock of it, since the framing is exactly the part a mock would agree with whatever it
did. The receiver answers nothing, so the fake records and says nothing too.

Skipped on Windows, where the door is a named pipe and asyncio cannot serve one; the wire is the same and the
write goes through a thread there, which is measured by nobody yet.
"""

import asyncio
import contextlib
import json
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from yaac import messaging_socket
from yaac.backend import Backend

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="the door is a named pipe there")

FORUM = "forum"
SETTLE = 0.4
TOKEN = "0123456789abcdef"


class FakeInbox:
    """What Claude Code listens on: takes lines, keeps them, answers nothing."""

    def __init__(self) -> None:
        # A Unix socket path is capped at 104 bytes on macOS, which a pytest tmp_path can exceed.
        self.path = str(Path(tempfile.mkdtemp()) / "inbox.sock")
        self.lines: list[dict] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._serve, self.path)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while line := await reader.readline():
            self.lines.append(json.loads(line))
        writer.close()

    def delivered(self) -> list[str]:
        """The text of every user message, in order."""
        return [line["message"]["content"] for line in self.lines if line.get("type") == "user"]


@pytest.fixture
async def inbox() -> AsyncIterator[FakeInbox]:
    fake = FakeInbox()
    await fake.start()
    try:
        yield fake
    finally:
        await fake.stop()


async def test_a_post_is_an_auth_line_then_a_user_message(inbox: FakeInbox) -> None:
    """The shape the receiver reads, and nothing else on the connection: a wrong `type` is dropped without a word,
    so this is the one place the shape is checked at all."""
    assert await messaging_socket.post("Привет, bob", path=inbox.path, token=TOKEN) is True
    await asyncio.sleep(SETTLE)
    assert inbox.lines == [
        {"type": "auth", "token": TOKEN},
        {"type": "user", "message": {"role": "user", "content": "Привет, bob"}},
    ]


@pytest.mark.parametrize(
    "environment,expected",
    [
        ({"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock", "CLAUDE_CODE_MESSAGING_TOKEN": "t"}, ("/tmp/x.sock", "t")),
        ({"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/x.sock"}, ("/tmp/x.sock", "")),
        ({"CLAUDE_CODE_MESSAGING_TOKEN": "t"}, None),
        ({}, None),
    ],
    ids=["both", "path-only", "token-only", "neither"],
)
def test_the_door_is_what_the_client_exported(monkeypatch, environment: dict, expected) -> None:
    """The path decides. A token alone is not a door, and a path without one is still posted to -- the token is
    optional on macOS and Linux, and a receiver that wants it will say nothing, which is the failure mode
    everywhere here."""
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    assert messaging_socket.door() == expected


async def test_a_door_that_is_not_there_is_not_an_error() -> None:
    assert await messaging_socket.post("anything", path=str(Path(tempfile.mkdtemp()) / "gone.sock")) is False


async def test_nothing_is_posted_without_a_door() -> None:
    """The autouse fixture has scrubbed the variables, which is the state of every client but Claude Code."""
    assert await messaging_socket.post("anything") is False


@pytest.mark.parametrize("hooked", [True, False], ids=["with-hook", "without-hook"])
async def test_an_arrival_rings_once_and_leaves_the_mail(endpoint: str, monkeypatch, inbox: FakeInbox, hooked) -> None:
    """Claude Code prints a post to this socket in the user's transcript, so it names who wrote and what about,
    never the body -- and takes nothing, since the hook or check_inbox delivers. Mail already named is not named
    again, so a second arrival rings for itself alone."""
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", inbox.path)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", TOKEN)

    listener, talker = Backend(endpoint), Backend(endpoint)
    listener.hooked = hooked
    try:
        await listener.connect(FORUM, "ann")
        await talker.connect(FORUM, "bob")
        assert listener.delivers is True
        await talker.resolve(None).send("first line\nSECRET-REST", name="ann", subject="schema renamed")
        await asyncio.sleep(SETTLE)
        await talker.resolve(None).send("\n  CI is   red\nSECRET-REST")
        await asyncio.sleep(SETTLE)

        [one, two] = inbox.delivered()
        # The first line is the one a person reads in the transcript: who, where, and what about.
        assert one.splitlines()[0] == f"[YAAC] bob on {FORUM!r}: schema renamed"
        assert two.splitlines()[0] == f"[YAAC] bob on {FORUM!r}: CI is red"
        assert "SECRET-REST" not in one + two
        # The receiver frames it as a message from another Claude Code session; a model that believed that would
        # answer with the wrong tool. And it says where the bodies are, which depends on whether a hook calls in.
        assert "send, not SendMessage" in one
        assert ("attaches the messages" in one) is hooked and ("check_inbox" in one) is not hooked
        assert [m["body"] for m in listener.resolve(None).receive()] == [
            "first line\nSECRET-REST",
            "\n  CI is   red\nSECRET-REST",
        ]
        assert inbox.lines[0] == {"type": "auth", "token": TOKEN}
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()


async def test_a_failed_post_leaves_the_mail_in_the_inbox(endpoint: str, monkeypatch) -> None:
    """The messages are taken only once the post has gone through, so a door that is gone loses nothing: the mail
    waits for check_inbox exactly as it would have on a client with no door at all."""
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(Path(tempfile.mkdtemp()) / "gone.sock"))

    listener, talker = Backend(endpoint), Backend(endpoint)
    try:
        await listener.connect(FORUM, "ann")
        await talker.connect(FORUM, "bob")
        await talker.resolve(None).send("still here", name="ann")
        await asyncio.sleep(SETTLE)

        [waiting] = listener.resolve(None).receive()
        assert waiting["body"] == "still here"
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()
