"""Waking a Codex session that is sitting idle, which a hook cannot do.

A hook fires when a session acts; this fires when mail arrives. It is an alarm clock rather than a postman --
`turn/start` begins a turn and `check_inbox` still does the reading -- so what is worth pinning down is that it
stays silent unless the user asked for it, that it says who is speaking, and that every way of failing is quiet.
"""

import asyncio
from typing import Any

import pytest

from yaac import wake
from yaac.backend import Backend

FORUM = "forum"
SETTLE = 0.4


@pytest.mark.parametrize(
    "value,wanted",
    [("1", True), ("yes", True), ("", False), (None, False)],
    ids=["one", "word", "empty", "unset"],
)
def test_waking_is_off_until_the_user_asks_for_it(monkeypatch, value: str | None, wanted: bool) -> None:
    """Starting a turn spends tokens and runs tools in somebody's session. That is not a decision a library takes
    on their behalf, so the default is silence and one environment variable is the whole of the opt-in."""
    monkeypatch.delenv(wake.WAKE_ENV, raising=False)
    if value is not None:
        monkeypatch.setenv(wake.WAKE_ENV, value)
    assert wake.wanted() is wanted


async def test_a_wake_asks_for_a_turn_in_the_named_thread(monkeypatch) -> None:
    """The whole request: a thread and some input. No `model`, `cwd` or `approvalPolicy` -- `turn/start` requires
    neither, so Codex answers those from the thread's own configuration rather than from our guess about it."""
    seen: dict[str, Any] = {}

    class Proxy:
        returncode = 0

        async def communicate(self, written: bytes) -> tuple[bytes, bytes]:
            seen["request"] = __import__("json").loads(written.decode())
            return b'{"jsonrpc":"2.0","id":1,"result":{}}\n', b""

    async def spawn(*command, **kwargs):
        seen["command"] = command
        return Proxy()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    assert await wake.wake("01THREAD", "mail arrived") is True
    assert seen["command"] == wake.PROXY  # the daemon is reached through codex itself, not a socket path we guess
    assert seen["request"]["method"] == "turn/start"
    assert seen["request"]["params"] == {
        "threadId": "01THREAD",
        "input": [{"type": "text", "text": "mail arrived"}],
    }


@pytest.mark.parametrize(
    "answer",
    [b'{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"no such thread"}}\n', b"", b"not json\n"],
    ids=["refused", "silence", "gibberish"],
)
async def test_every_way_of_failing_is_quiet(monkeypatch, answer: bytes) -> None:
    """A session not running under the app-server daemon has no thread to find. That is not an error to report to
    anybody: the mail is in the inbox, and a session that cannot be woken is one that reads it next time it acts."""

    class Proxy:
        returncode = 0

        async def communicate(self, written: bytes) -> tuple[bytes, bytes]:
            return answer, b""

    monkeypatch.setattr(asyncio, "create_subprocess_exec", lambda *a, **k: _resolved(Proxy()))
    assert await wake.wake("01THREAD", "mail arrived") is False


async def test_a_missing_codex_is_not_an_error(monkeypatch) -> None:
    """`codex` may not be on PATH at all -- this is a Claude Code session as often as not."""

    async def missing(*command, **kwargs):
        raise FileNotFoundError("codex")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing)
    assert await wake.wake("01THREAD", "mail arrived") is False


def _resolved(value):
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    future.set_result(value)
    return future


async def test_an_arrival_wakes_the_session_once(endpoint: str, monkeypatch) -> None:
    """One wake drains any number of messages, because reading is a pull -- so a second arrival while the first
    wake is still in flight needs no policy to coalesce it, and gets none."""
    monkeypatch.setenv(wake.WAKE_ENV, "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"wake-{endpoint.rsplit(':', 1)[1]}")
    woken: list[tuple[str, str]] = []

    async def record(thread: str, text: str, timeout: float = 0) -> bool:
        woken.append((thread, text))
        await asyncio.sleep(0.2)
        return True

    monkeypatch.setattr(wake, "wake", record)

    listener, talker = Backend(endpoint), Backend(endpoint)
    try:
        await listener.connect(FORUM, "ann")
        listener.notices.thread = "01THREAD"  # what the hook would have told it
        await talker.connect(FORUM, "bob")
        await talker.resolve(None).send("first", name="ann")
        await talker.resolve(None).send("second", name="ann")
        await asyncio.sleep(SETTLE)

        assert len(woken) == 1
        thread, text = woken[0]
        assert thread == "01THREAD"
        # It says who is speaking: an alarm that reads like the user talking is worse than no alarm.
        assert "check_inbox" in text and "Nobody typed this" in text
        assert FORUM in text
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()


async def test_nothing_is_woken_without_a_thread_or_an_opt_in(endpoint: str, monkeypatch) -> None:
    """Two independent conditions, because either alone would be a session woken by surprise."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", f"quiet-{endpoint.rsplit(':', 1)[1]}")
    woken: list[str] = []

    async def record(thread: str, text: str, timeout: float = 0) -> bool:
        woken.append(thread)
        return True

    monkeypatch.setattr(wake, "wake", record)

    listener, talker = Backend(endpoint), Backend(endpoint)
    try:
        await listener.connect(FORUM, "ann")
        await talker.connect(FORUM, "bob")

        monkeypatch.setenv(wake.WAKE_ENV, "1")  # asked for, but nothing knows how to reach this session
        listener.notices.thread = None
        await talker.resolve(None).send("no thread", name="ann")
        await asyncio.sleep(SETTLE)

        monkeypatch.delenv(wake.WAKE_ENV, raising=False)  # reachable, but nobody asked
        listener.notices.thread = "01THREAD"
        await talker.resolve(None).send("no opt-in", name="ann")
        await asyncio.sleep(SETTLE)

        assert woken == []
    finally:
        await listener.disconnect_all()
        await talker.disconnect_all()
        listener.close()
        talker.close()
