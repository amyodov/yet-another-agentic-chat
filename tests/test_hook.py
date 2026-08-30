"""The hook: the one path that delivers without being asked, on both clients that have one.

The hook calls a tool on the server that already holds the inbox, so there is no second process and nothing on the
wire to test. What is worth pinning down is the shape of the answer -- the client reads it as a hook decision, not
as a tool result, and Claude Code and Codex end a turn through different doors -- that the delivery really is a
delivery, and that the tool stays out of every listing.
"""

import asyncio
import io
import json
import sys
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from yaac import frontend, protocol
from yaac import hook as yaac_hook
from yaac.backend import Backend
from yaac.protocol import Address, Scope

FORUM = "forum"
OTHER = "standup"
SETTLE = 0.4  # generous for loopback TCP, short enough to keep the suite quick

RadioFactory = Callable[[], Backend]


def aloud(sender: str, body: str, **kwargs) -> dict[str, Any]:
    """A broadcast as the hat would have handed it over, built by the real constructors so these cases cannot
    drift from what the wire carries."""
    return protocol.message(
        Scope(channel=FORUM), frm=Scope(channel=FORUM, peer=Address(sender)), body=body, **kwargs
    ).to_wire()


def whispered(sender: str, recipient: str, body: str) -> dict[str, Any]:
    return protocol.message(
        Scope(channel=FORUM, peer=Address(recipient)),
        frm=Scope(channel=FORUM, peer=Address(sender)),
        body=body,
    ).to_wire()


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


async def hook(event: str = "Stop", tool_name: str = "", client: str = "claude-code") -> dict[str, Any]:
    """What the client would read back from the hook, parsed."""
    await asyncio.sleep(SETTLE)
    return json.loads(await frontend.hook_report(event=event, tool_name=tool_name, client=client))


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
        (aloud("bob", "hello"), "  · bob → everyone: hello"),
        (whispered("bob", "ann", "psst"), "  · bob → you: psst"),
        (protocol.message(Scope(channel=FORUM), body="orphan").to_wire(), "  · someone → everyone: orphan"),
        (aloud("Колян", "привет"), "  · Колян → everyone: привет"),
        (aloud("bob", "a\nb"), "  · bob → everyone: a\nb"),
        (
            aloud("bob", "who owns this?", mentions=(Address("ann"),)),
            "  · bob → everyone (calling on ann): who owns this?",
        ),
        (
            protocol.bounce("01J", "no such recipient", to=Scope(peer=Address("ann"))).to_wire(),
            "  · undelivered: no such recipient",
        ),
        (
            protocol.error("name taken", to=Scope(peer=Address("ann"))).to_wire(),
            "  · refused: name taken",
        ),
    ],
    ids=["broadcast", "whisper", "no-sender", "non-ascii", "newline", "mention", "bounce", "error"],
)
def test_every_kind_of_inbox_entry_has_a_line(message: dict[str, Any], line: str) -> None:
    """Bounces and refusals sit in the same inbox as messages and matter as much -- a send that failed is news."""
    assert frontend._shown(message) == line


def test_a_body_is_delivered_whole(radios) -> None:
    """Nothing is trimmed, because the messages are taken from the inbox as they are shown: anything held back
    would be held back for good, where check_inbox would have handed over all of it."""
    body = "x" * 50_000
    assert frontend._shown(aloud("bob", body)).endswith(body)


@pytest.mark.parametrize(
    "client,event,continuation",
    [
        ("claude-code", "Stop", False),
        ("claude-code", "PreToolUse", False),
        ("codex", "PreToolUse", False),
        ("codex", "Stop", True),
        ("codex", "SubagentStop", True),
    ],
)
async def test_each_client_is_answered_in_the_contract_it_reads(
    radios: RadioFactory, client: str, event: str, continuation: bool
) -> None:
    """Same messages, two envelopes. Codex's Stop output schema admits no `hookSpecificOutput`, and puts text in
    front of the model as `decision: "block"` with a `reason` it turns into a continuation prompt; everywhere else
    both clients read `additionalContext`. `Stop` is spelled the same in both, so the caller says which contract it
    speaks rather than having it inferred from the event name."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("the field is recipient_group now")

    reply = await hook(event, client=client)
    if continuation:
        assert reply["decision"] == "block"
        context = reply["reason"]
    else:
        assert reply["hookSpecificOutput"]["hookEventName"] == event
        context = reply["hookSpecificOutput"]["additionalContext"]
    assert "bob → everyone: the field is recipient_group now" in context
    # Whichever door it came through, it was a delivery: the inbox agrees.
    assert listener.resolve(None).pending_count() == 0


@pytest.mark.parametrize("event", ["SessionStart", "PostCompact"])
async def test_a_replaced_context_is_handed_back_what_it_was_holding(radios: RadioFactory, event: str) -> None:
    """A compaction takes the connection id and the secret with it, leaving a session on the air and mute.

    Claude Code adds a `SessionStart` hook's plain text to the context it starts the next turn with, and fires it
    with `source: compact`. The hook runs in the process that owns the memberships, so nothing is recovered or
    stored -- what the model lost is simply read back off state this process held all along.
    """
    listener = radios()
    first = await listener.connect(FORUM, "ann")
    second = await listener.connect(OTHER, "deputy")

    said = (await hook(event))["hookSpecificOutput"]["additionalContext"]
    for held in (first, second):
        assert held.connection_id in said and held.peer_secret in said and held.peer_uid in said
    # And what to do when it happens again, since the hook cannot promise to fire.
    assert "join_channel" in said and "check_inbox" in said


async def test_a_session_holding_nothing_is_told_nothing(radios: RadioFactory) -> None:
    """The hook fires on every compaction, including in a session that never joined a channel -- where a report
    about connections it does not have is noise charged to somebody's context."""
    radios()
    assert (await hook("SessionStart")) == {"suppressOutput": True}


async def test_the_mail_is_left_for_check_inbox_when_the_context_was_replaced(radios: RadioFactory) -> None:
    """A `Stop` hook delivers and consumes; this one must not. A model that has just lost its secret cannot act
    on a message yet, so the mail stays in the inbox and the text asks for the `check_inbox` that reads it."""
    listener = radios()
    await listener.connect(FORUM, "ann")
    talker = radios()
    await talker.connect(FORUM, "bob")
    await talker.resolve(None).send("still here?")

    await hook("SessionStart")
    assert listener.resolve(None).pending_count() == 1


def _stdin(payload: dict[str, Any], encoding: str = "cp1251"):
    """A hook payload as a client actually hands it over: UTF-8 bytes, under a text layer decoding with whatever
    the console chose. `cp1251` is the default on a Russian Windows, and is the case Vadim met."""
    return io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding=encoding)


@pytest.mark.parametrize(
    "payload,speaks",
    [
        ({"hook_event_name": "Stop", "stop_hook_active": True}, False),
        ({"hook_event_name": "Stop", "tool_name": "mcp__yaac__check_inbox"}, False),
        ({"hook_event_name": "Stop"}, True),
    ],
    ids=["already-continued", "check_inbox-is-next", "first-time"],
)
def test_the_out_of_process_hook_says_it_once(monkeypatch, payload: dict[str, Any], speaks: bool, capsys) -> None:
    """It reports a count rather than delivering, so nothing it does empties the inbox and a `Stop` that blocks
    would find the same mail on the continuation it caused. Measured before this guard existed: the session
    continued itself until it was killed. `stop_hook_active` is what Codex sets to break exactly that."""
    waiting = {"connections": [{"channel": "forum", "name": "ann", "unread": 2}]}
    # The hook now finds its own session through the directory the hat serves, then asks that session's socket.
    # Both are stubbed: what is under test is whether it speaks once per turn, not how it finds anybody.
    monkeypatch.setattr(yaac_hook, "directory", lambda: [{"pid": 1, "cwd": "/here", "watch": "ws://127.0.0.1:1/x"}])
    monkeypatch.setattr(yaac_hook, "ask", lambda url, thread=None: {"session": None, **waiting})
    monkeypatch.setattr(sys, "stdin", _stdin({"cwd": "/here", **payload}))
    yaac_hook.main()
    assert ("decision" in capsys.readouterr().out) == speaks


@pytest.mark.parametrize("cwd", ["/дом", "/家", "/plain"], ids=["cyrillic", "han", "ascii"])
def test_the_payload_is_read_as_utf8_whatever_the_console_says(monkeypatch, cwd: str, capsys) -> None:
    """A hook payload is UTF-8 by its client's contract, and `sys.stdin` decodes with the console's encoding
    instead -- so on a Russian Windows the directory a session was started in came back as a different string,
    and the hook matched nobody. Reading the buffer is what keeps a path or a name intact through the one place
    it crosses a process boundary as text.
    """
    monkeypatch.setattr(yaac_hook, "directory", lambda: [{"pid": 1, "cwd": cwd, "watch": "ws://127.0.0.1:1/x"}])
    monkeypatch.setattr(
        yaac_hook, "ask", lambda url, thread=None: {"connections": [{"channel": "форум", "name": "аня", "unread": 1}]}
    )
    monkeypatch.setattr(sys, "stdin", _stdin({"cwd": cwd, "hook_event_name": "Stop"}))
    yaac_hook.main()

    spoken = json.loads(capsys.readouterr().out)
    assert "1 on 'форум', to you as 'аня'" in spoken["reason"]


@pytest.mark.parametrize("client", ["claude-code", "codex"])
async def test_nothing_to_say_is_said_as_nothing(radios: RadioFactory, client: str) -> None:
    """The hook runs before every tool call, so silence is the common case and it has to be cheap and clean --
    `suppressOutput` is a valid decision, where an empty string or a bare `{}` would be read as stray output. It is
    the one answer both contracts share: Codex's Stop schema carries the field as well, so silence needs no
    dialect."""
    # Codex rejects `suppressOutput` on PreToolUse and accepts an empty object everywhere, so silence has a
    # dialect after all -- measured against codex-cli 0.147.0, which reports the hook as failed otherwise.
    expected = {} if client == "codex" else {"suppressOutput": True}
    assert json.loads(await frontend.hook_report(event="Stop", client=client)) == expected
    quiet = radios()
    await quiet.connect(FORUM, "ann")
    assert await hook("PreToolUse", client=client) == expected
