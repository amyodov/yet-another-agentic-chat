"""The terminal client, driven headlessly against a real net.

Textual's `run_test()` runs the actual app with a real terminal size and real key handling, so these exercise the
whole path -- keystroke, backend, hat, back into the widget -- rather than calling methods on a stub.
"""

import asyncio

import pytest

from yaac.backend import Backend
from yaac.chat import ChatApp


def transcript(app: ChatApp) -> str:
    """Everything written to the log, flattened. RichLog keeps the renderables it was handed."""
    return "\n".join(str(line) for line in app.query_one("#log").lines)


def status(app: ChatApp) -> str:
    return str(app.query_one("#status").content)


async def settle(pilot, seconds: float = 0.4) -> None:
    """Give the hat time to route and the roster time to arrive. There is no ack to wait on in v0."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        await pilot.pause()
        await asyncio.sleep(0.02)


async def test_a_person_and_a_peer_exchange_messages(endpoint: str) -> None:
    """The client is an ordinary participant: what it sends reaches a backend peer, and what that peer sends
    appears without anything having asked for it."""
    peer = Backend(endpoint)
    await peer.connect("forum", "bob")
    try:
        app = ChatApp(endpoint, "forum", "ann")
        async with app.run_test() as pilot:
            await settle(pilot)
            assert app.membership is not None
            assert app.present == ["bob"]

            await pilot.press(*"hello bob", "enter")
            await settle(pilot)
            [received] = peer.resolve(None).receive()
            assert received["body"] == "hello bob"
            assert received["from"]["name"] == "ann"

            await peer.resolve(None).send("hello ann", "ann")
            await settle(pilot)
            assert "hello ann" in transcript(app)
    finally:
        await peer.disconnect_all()
        peer.close()


async def test_arrivals_and_departures_are_written_into_the_history(endpoint: str) -> None:
    """A single-column window answers "who is here" from the transcript, so presence has to be an event in it.
    The hat broadcasts a roster on every hello and eviction, which is what makes this push rather than polling."""
    app = ChatApp(endpoint, "forum", "ann")
    async with app.run_test() as pilot:
        await settle(pilot)
        assert app.present == []
        assert "alone here" in status(app)

        latecomer = Backend(endpoint)
        await latecomer.connect("forum", "bob")
        try:
            await settle(pilot)
            assert "bob is here" in transcript(app)
            assert "with bob" in status(app)
        finally:
            await latecomer.disconnect_all()
            latecomer.close()


@pytest.mark.parametrize(
    "keys,expected",
    [
        ([], "chat"),
        (["left"], "channels"),
        (["right"], "members"),
        (["right", "escape"], "chat"),
        (["tab"], "members"),
        (["tab", "tab"], "channels"),
        (["shift+tab"], "channels"),
    ],
    ids=["start", "left-out", "right-in", "escape", "tab", "tab-twice", "shift-tab"],
)
async def test_the_modes_sit_on_one_axis(endpoint: str, keys: list[str], expected: str) -> None:
    """Left goes out, right goes in, Tab walks the same order. One rule, so there is nothing to memorise."""
    app = ChatApp(endpoint, "forum", "ann")
    async with app.run_test() as pilot:
        await settle(pilot, 0.2)
        for key in keys:
            await pilot.press(key)
            await pilot.pause()
        assert app.mode == expected


async def test_arrows_navigate_only_when_there_is_no_text_to_move_through(endpoint: str) -> None:
    """The empty prompt is what makes the gesture free. With a draft in the line, left and right are a cursor
    again -- otherwise the roster would cost you your sentence."""
    app = ChatApp(endpoint, "forum", "ann")
    async with app.run_test() as pilot:
        await settle(pilot, 0.2)
        await pilot.press(*"draft")
        await pilot.press("left")
        await pilot.pause()
        assert app.mode == "chat"
        assert app.query_one("#prompt").value == "draft"

        # Tab has no such guard, which is the point of having it: the roster stays reachable mid-sentence.
        await pilot.press("tab")
        await pilot.pause()
        assert app.mode == "members"
        assert app.query_one("#prompt").value == "draft"


async def test_picking_a_peer_makes_the_next_message_a_whisper(endpoint: str) -> None:
    """Members is a picker with a consequence, not a display: choosing someone is how you address them, and
    'everyone' is a row you have to choose rather than the state you fall into."""
    peer = Backend(endpoint)
    await peer.connect("forum", "bob")
    try:
        app = ChatApp(endpoint, "forum", "ann")
        async with app.run_test() as pilot:
            await settle(pilot)
            await pilot.press("right")
            await pilot.pause()
            await pilot.press("down", "enter")  # past "everyone", onto bob
            await settle(pilot, 0.2)
            assert app.recipient == "bob"
            assert app.mode == "chat"
            assert "to bob" in status(app)

            await pilot.press(*"psst", "enter")
            await settle(pilot)
            [whisper] = peer.resolve(None).receive()
            assert whisper["to"]["name"] == "bob"
    finally:
        await peer.disconnect_all()
        peer.close()
