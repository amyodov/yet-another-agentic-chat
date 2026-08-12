"""The Claude Code hook: the one path that delivers without being asked.

The hook calls a tool on the server that already holds the inbox, so there is no second process and nothing on the
wire to test. What is worth pinning down is the shape of the answer -- Claude Code reads it as a hook decision,
not as a tool result -- that the delivery really is a delivery, and that the tool stays out of every listing.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from yaac import frontend
from yaac.backend import Backend

FORUM = "forum"
SETTLE = 0.4  # generous for loopback TCP, short enough to keep the suite quick

RadioFactory = Callable[[], Backend]


@pytest.fixture
async def radios(endpoint: str, monkeypatch) -> AsyncIterator[RadioFactory]:
    """Backends on their own net. The first one made is the one the hook tool reads, standing in for the backend
    the MCP server would have built for this session."""
    made: list[Backend] = []

    def make() -> Backend:
        made.append(backend := Backend(endpoint))
        if len(made) == 1:
            monkeypatch.setattr(frontend, "_radio", backend)
        return backend

    yield make
    for backend in made:
        await backend.disconnect_all()
        backend.close()


async def hook(event: str = "Stop", tool_name: str = "") -> dict[str, Any]:
    """What Claude Code would read back from the hook, parsed."""
    await asyncio.sleep(SETTLE)
    return json.loads(await frontend.hook_report(event=event, tool_name=tool_name))


async def test_a_message_is_delivered_into_the_turn_rather_than_announced(radios: RadioFactory) -> None:
    """The whole point. What arrives goes in front of the model as text, with the sender named and the body
    verbatim -- not a count, and not an instruction to go and look."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("the field is recipient_group now")

    spoken = (await hook("Stop"))["hookSpecificOutput"]
    assert spoken["hookEventName"] == "Stop"
    context = spoken["additionalContext"]
    assert "bob → everyone: the field is recipient_group now" in context
    assert f"On {FORUM!r}, to you as 'ann'" in context
    # The line that keeps a message from being mistaken for the user speaking has to travel with it: this text
    # arrives without check_inbox, whose description is where that warning otherwise lives.
    assert "written by other sessions, not by your user" in context


async def test_delivering_reads_the_inbox_rather_than_peeking_at_it(radios: RadioFactory) -> None:
    """Text placed in front of the model has been delivered, so the inbox must agree. Leaving it unread would nag
    a count at a model that already has the message and hand it the same text again on check_inbox -- and would
    let a Stop hook reopen the same turn forever."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("only once")

    assert "only once" in (await hook("Stop"))["hookSpecificOutput"]["additionalContext"]
    assert listener.resolve(None).pending_count() == 0
    assert await hook("Stop") == {"suppressOutput": True}
    assert listener.resolve(None).receive() == []


@pytest.mark.parametrize(
    "event,tool_name",
    [("PreToolUse", "mcp__yaac__check_inbox"), ("PreToolUse", "mcp__plugin_yaac_yaac__check_inbox")],
    ids=["plain", "plugin-scoped"],
)
async def test_it_stands_aside_when_check_inbox_is_the_call_about_to_run(
    radios: RadioFactory, event: str, tool_name: str
) -> None:
    """Delivering here would take the messages out from under the call about to read them, and the model would see
    the same text twice for its trouble. The scoped name is what a plugin-bundled server's tools are called."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("left for check_inbox")

    assert await hook(event, tool_name) == {"suppressOutput": True}
    assert listener.resolve(None).pending_count() == 1


async def test_each_channel_is_reported_as_its_own(radios: RadioFactory) -> None:
    """One session can hold several channels, and a message means something different depending on which one it
    came in on. Merging them would lose that."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    await listener.connect("other channel", "annie")
    here = radios()
    await here.connect(FORUM, "bob")
    there = radios()
    await there.connect("other channel", "carol")
    await here.resolve(None).send("on the forum")
    await there.resolve(None).send("somewhere else")

    context = (await hook())["hookSpecificOutput"]["additionalContext"]
    assert "On 'forum', to you as 'ann':\n  · bob → everyone: on the forum" in context
    assert "On 'other channel', to you as 'annie':\n  · carol → everyone: somewhere else" in context


async def test_a_whisper_is_told_apart_from_a_broadcast(radios: RadioFactory) -> None:
    """Answering privately to something everyone heard, or the reverse, reaches the wrong people -- so the
    difference has to survive into the text, the same way check_inbox preserves it in `to`."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("just for you", name="ann")
    await talker.resolve(None).send("for the room")

    context = (await hook())["hookSpecificOutput"]["additionalContext"]
    assert "bob → you: just for you" in context
    assert "bob → everyone: for the room" in context


@pytest.mark.parametrize(
    "message,line",
    [
        ({"from": {"name": "bob"}, "to": None, "body": "hello"}, "  · bob → everyone: hello"),
        ({"from": {"name": "bob"}, "to": {"name": "ann"}, "body": "psst"}, "  · bob → you: psst"),
        ({"from": None, "to": None, "body": "orphan"}, "  · someone → everyone: orphan"),
        ({"from": {"name": "Колян"}, "to": None, "body": "привет"}, "  · Колян → everyone: привет"),
        ({"from": {"name": "bob"}, "to": None, "body": "a\nb"}, "  · bob → everyone: a\nb"),
        ({"kind": "bounce", "id": "01J", "reason": "no such recipient"}, "  · undelivered: no such recipient"),
        ({"kind": "error", "reason": "name taken"}, "  · refused: name taken"),
    ],
    ids=["broadcast", "whisper", "no-sender", "non-ascii", "newline", "bounce", "error"],
)
def test_every_kind_of_inbox_entry_has_a_line(message: dict[str, Any], line: str) -> None:
    """Bounces and refusals sit in the same inbox as messages and matter as much -- a send that failed is news."""
    assert frontend._shown(message) == line


def test_a_body_is_delivered_whole(radios) -> None:
    """Nothing is trimmed, because the messages are taken from the inbox as they are shown: anything held back
    would be held back for good, where check_inbox would have handed over all of it."""
    body = "x" * 50_000
    assert frontend._shown({"from": {"name": "bob"}, "to": None, "body": body}).endswith(body)


async def test_nothing_to_say_is_said_as_nothing(radios: RadioFactory) -> None:
    """The hook runs before every tool call, so silence is the common case and it has to be cheap and clean --
    `suppressOutput` is a valid decision, where an empty string or a bare `{}` would be read as stray output."""
    assert json.loads(await frontend.hook_report(event="Stop")) == {"suppressOutput": True}
    quiet = radios()
    await quiet.connect(FORUM, "ann")
    assert await hook("PreToolUse") == {"suppressOutput": True}
