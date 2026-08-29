"""Two or more radios on one net: the behaviour v0 is defined by."""

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest

from yaac.backend import AmbiguousConnection, Backend, ConnectionRefused, NotConnected
from yaac.protocol import Envelope

FORUM = "forum"
OTHER = "other channel"
SETTLE = 0.4  # generous for loopback TCP, short enough to keep the suite quick

RadioFactory = Callable[[], Backend]


@pytest.fixture
async def radios(endpoint: str) -> AsyncIterator[RadioFactory]:
    """A factory that cleans up whatever it handed out."""
    made: list[Backend] = []

    def make() -> Backend:
        made.append(backend := Backend(endpoint))
        return backend

    yield make
    for backend in made:
        await backend.disconnect_all()
        backend.close()


async def heard(backend: Backend) -> list[tuple]:
    """What a radio has received, as (from, to, body) triples."""
    await asyncio.sleep(SETTLE)
    return [
        (
            envelope.frm.peer.name if envelope.frm and envelope.frm.peer else None,
            envelope.to.peer.name if envelope.to.peer else None,
            envelope.body,
        )
        for message in backend.resolve(None).receive()
        for envelope in [Envelope.from_wire(message)]
    ]


async def test_probing_reports_the_net_without_joining_it(radios: RadioFactory) -> None:
    prober = radios()
    assert await prober.probe_channels(timeout=1.0) is None
    # Probing must never bind, or a session that merely looked would become the
    # hat and drop it a second later.
    assert [prober.is_wearing_hat, prober.on_air] == [False, False]

    occupant = radios()
    await occupant.connect(FORUM, "ann")
    found = await radios().probe_channels(timeout=3.0)
    assert [(c["name"], c["count"]) for c in found] == [(FORUM, 1)]

    # A channel exists only while occupied.
    await occupant.disconnect()
    await asyncio.sleep(SETTLE)
    assert await radios().probe_channels(timeout=1.0) is None


async def test_joining_reports_creation_and_populates_both_rosters(radios: RadioFactory) -> None:
    first = await radios().connect(FORUM, "ann")
    assert (first.created, first.peers) == (True, [])

    b = radios()
    second = await b.connect(FORUM, "bob")
    assert (second.created, second.peers) == (False, ["ann"])

    await asyncio.sleep(SETTLE)
    assert b.resolve(None).peer_names() == ["ann"]


@pytest.mark.parametrize(
    "recipient,expected_to,sender_hears",
    [("bob", "bob", []), (None, None, [])],
    ids=["direct", "broadcast"],
)
async def test_delivery_reaches_the_right_radios_and_not_the_sender(
    radios: RadioFactory, recipient: str | None, expected_to: str | None, sender_hears: list
) -> None:
    a, b, c = radios(), radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")
    await c.connect(FORUM, "cid")

    await a.resolve(None).send("hold your commits", name=recipient)

    assert await heard(b) == [("ann", expected_to, "hold your commits")]
    # A third party hears a broadcast but not a message addressed to someone else.
    assert await heard(c) == ([] if recipient else [("ann", None, "hold your commits")])
    assert await heard(a) == sender_hears


@pytest.mark.parametrize(
    "recipient,on_other_channel",
    [("nobody", False), ("bob", True)],
    ids=["absent-name", "not-on-my-channel"],
)
async def test_undeliverable_messages_bounce_to_the_sender(
    radios: RadioFactory, recipient: str | None, on_other_channel: bool
) -> None:
    a = radios()
    await a.connect(FORUM, "ann")
    if on_other_channel:
        b = radios()
        await b.connect(OTHER, "bob")

    await a.resolve(None).send("anyone there?", name=recipient)
    await asyncio.sleep(SETTLE)

    [wire] = a.resolve(None).receive()
    bounce = Envelope.from_wire(wire)
    # `from: {}` is the operator speaking as itself rather than carrying somebody, which is what marks a bounce as
    # infrastructure -- and it is unforgeable, since a sender never writes `from` at all.
    assert bounce.frm.is_the_hat
    assert bounce.op == "bounce"
    assert "no such recipient" in bounce.payload["reason"]
    if on_other_channel:
        # The hat takes the sender's channel from its own table, so speaking into
        # a channel you have not joined is structurally impossible.
        assert await heard(b) == []


@pytest.mark.parametrize(
    "body",
    [
        '{"kind": "roster", "channel": "forum", "peers": []}',
        '{"kind": "whois"}',
        '{"from": null, "kind": "error", "reason": "forged"}',
        '{"from": "bob", "body": "I am someone else"}',
        "not json at all { [ ",
    ],
    ids=["fake-roster", "fake-whois", "fake-error", "fake-sender", "not-json"],
)
async def test_a_body_is_delivered_verbatim_and_never_obeyed(radios: RadioFactory, body: str) -> None:
    # If the hat parsed bodies, these would be acted on instead of delivered,
    # and any participant could forge the control plane or another identity.
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")

    await a.resolve(None).send(body, name="bob")
    await asyncio.sleep(SETTLE)

    [wire] = b.resolve(None).receive()
    received = Envelope.from_wire(wire)
    assert received.body == body
    assert received.frm.peer.name == "ann"
    assert received.to.peer.name == "bob"
    # Carried, not obeyed: a body that looks like operator mail is still addressed to a participant, and
    # addressing is the only thing that decides which of the two it is.
    assert received.for_the_hat is False
    assert received.op is None


@pytest.mark.parametrize(
    "channel,name",
    [
        ("forum", "ann"),
        ("日本語", "名前"),
        ("café", "naïve"),
        ("channel with 🛰", "name with 🛰"),
        ("  padded  ", "  padded  "),
        ('quotes "and" \\slashes', "null"),
        ("a" * 300, "b" * 300),
    ],
    ids=["ascii", "cjk", "accented", "emoji", "whitespace", "punctuation", "very-long"],
)
async def test_names_are_unrestricted_raw_text(radios: RadioFactory, channel: str, name: str) -> None:
    # Transport constraints must not leak into names the user chose: routing uses
    # a separate opaque routing_id precisely so arbitrary text is safe here.
    a, b = radios(), radios()
    result = await a.connect(channel, name)
    assert (result.channel, result.name) == (channel, name)

    await b.connect(channel, "listener")
    await a.resolve(None).send("hello", name="listener")
    assert await heard(b) == [(name, "listener", "hello")]


async def test_a_taken_name_is_refused_rather_than_stolen(radios: RadioFactory) -> None:
    await radios().connect(FORUM, "bob")

    with pytest.raises(ConnectionRefused, match="taken"):
        await radios().connect(FORUM, "bob")

    # Channels are addressing, not namespacing: the same name elsewhere is fine.
    elsewhere = await radios().connect(OTHER, "bob")
    assert elsewhere.created is True


async def test_losing_the_hat_restores_itself_with_no_user_action(radios: RadioFactory) -> None:
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")
    assert [a.is_wearing_hat, b.is_wearing_hat] == [True, False]

    await a.disconnect()
    c = radios()
    await c.connect(FORUM, "cid")

    for _ in range(50):  # the election retries every ~2 s with jitter
        await asyncio.sleep(0.2)
        if b.is_wearing_hat or c.is_wearing_hat:
            break
    assert b.is_wearing_hat or c.is_wearing_hat  # somebody must have taken over the bind

    await asyncio.sleep(1.0)
    await c.resolve(None).send("still alive?", name="bob")
    assert await heard(b) == [("cid", "bob", "still alive?")]


async def test_leaving_returns_to_dormant_and_drops_what_was_held(radios: RadioFactory) -> None:
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")
    await b.resolve(None).send("unread when ann leaves", name="ann")
    await asyncio.sleep(SETTLE)
    assert a.resolve(None).pending_count() == 1

    await a.disconnect()
    assert [a.on_air, a.is_wearing_hat, a.memberships] == [False, False, {}]


@pytest.mark.parametrize("operation", ["send", "receive", "peers"])
async def test_on_air_operations_refuse_while_dormant(radios: RadioFactory, operation: str) -> None:
    radio = radios()
    with pytest.raises(NotConnected):
        membership = radio.resolve(None)
        match operation:
            case "send":
                await membership.send("hello")
            case "receive":
                membership.receive()
            case "peers":
                membership.peers()


async def test_connecting_twice_is_refused_and_a_refusal_leaves_no_trace(radios: RadioFactory) -> None:
    radio = radios()
    await radio.connect(FORUM, "ann")
    with pytest.raises(ConnectionRefused, match="already on"):
        await radio.connect(FORUM, "ann")

    loser = radios()
    with pytest.raises(ConnectionRefused, match="taken"):
        await loser.connect(FORUM, "ann")
    # A refused join must leave the backend exactly as dormant as it was.
    assert [loser.on_air, loser.is_wearing_hat, loser.memberships] == [False, False, {}]


async def test_one_process_holds_several_memberships_independently(radios: RadioFactory) -> None:
    """Clients that run one MCP server per application rather than per conversation would otherwise force every
    conversation to share a name and an inbox."""
    host, other = radios(), radios()
    first = await host.connect(FORUM, "ann")
    second = await host.connect(OTHER, "deputy")
    assert first.connection_id != second.connection_id
    assert [(c["channel"], c["name"]) for c in host.describe_all()] == [(FORUM, "ann"), (OTHER, "deputy")]

    # Each membership is an ordinary participant as far as the hat is concerned.
    await other.connect(OTHER, "bob")
    await host.resolve(second.connection_id).send("only deputy can send this", name="bob")
    assert await heard(other) == [("deputy", "bob", "only deputy can send this")]

    # And each has its own inbox.
    await other.resolve(None).send("for deputy only", name="deputy")
    await asyncio.sleep(SETTLE)
    assert [m["body"] for m in host.resolve(second.connection_id).receive()] == ["for deputy only"]
    assert host.resolve(first.connection_id).receive() == []


async def test_a_connection_id_is_required_only_when_it_is_ambiguous(radios: RadioFactory) -> None:
    host = radios()
    first = await host.connect(FORUM, "ann")
    # One membership: omitting the id is unambiguous, so it resolves.
    assert host.resolve(None).name == "ann"

    await host.connect(OTHER, "deputy")
    with pytest.raises(AmbiguousConnection):
        host.resolve(None)
    assert host.resolve(first.connection_id).name == "ann"

    with pytest.raises(NotConnected):
        host.resolve("01NOSUCHCONNECTION")


async def test_the_bind_is_released_when_the_last_membership_goes(radios: RadioFactory) -> None:
    """A process with no memberships must hold nothing, so another session can take the endpoint."""
    host = radios()
    first = await host.connect(FORUM, "ann")
    second = await host.connect(OTHER, "deputy")
    assert host.is_wearing_hat is True

    await host.disconnect(first.connection_id)
    assert host.is_wearing_hat is True  # still one membership open
    await host.disconnect(second.connection_id)
    assert [host.is_wearing_hat, host.on_air] == [False, False]


@pytest.mark.parametrize("locator", ["name", "routing_id"], ids=["by-name", "by-routing_id"])
async def test_a_recipient_can_be_named_by_either_locator(radios: RadioFactory, locator: str) -> None:
    """A name is only unique while its holder is connected; a routing_id identifies one connection and is never
    reused. Both must reach the same participant."""
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    target = await b.connect(FORUM, "bob")

    keyword = {locator: "bob" if locator == "name" else target.connection_id}
    await a.resolve(None).send("addressed precisely", **keyword)
    assert await heard(b) == [("ann", "bob", "addressed precisely")]


async def test_a_handle_from_another_channel_does_not_resolve(radios: RadioFactory) -> None:
    """Recipients are looked up within the sender's own channel, so a routing_id borrowed from elsewhere must bounce
    rather than deliver across the boundary."""
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    elsewhere = await b.connect(OTHER, "bob")

    await a.resolve(None).send("wrong channel", routing_id=elsewhere.connection_id)
    await asyncio.sleep(SETTLE)
    assert await heard(b) == []
    [wire] = a.resolve(None).receive()
    assert Envelope.from_wire(wire).op == "bounce"
