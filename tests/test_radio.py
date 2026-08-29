"""Two or more radios on one net: the behaviour v0 is defined by."""

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from yaac import frontend
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


async def test_joining_a_name_this_process_already_holds_hands_it_back(radios: RadioFactory) -> None:
    """Not a refusal, and the reason is a failure seen in the wild hours after 0.5.0 shipped.

    A model's context was compacted, taking the pair `join_channel` had returned with it. `send`, `peers` and
    `check_inbox` then refused -- correctly, since it could not present the secret -- and joining again was
    refused too, because the name was still held. The session was left deaf, mute, and unable to reclaim a name
    nobody else could take, with no way back at all.

    So a join that names a membership this process already holds returns it, secret and all. Nothing is given
    away: the caller is inside the process that owns it, and already had the connection id.
    """
    radio = radios()
    first = await radio.connect(FORUM, "ann")
    again = await radio.connect(FORUM, "ann")
    assert (again.connection_id, again.peer_secret) == (first.connection_id, first.peer_secret)
    assert len(radio.memberships) == 1

    # Somebody else asking for that name is still refused, which is the part that was always right.
    loser = radios()
    with pytest.raises(ConnectionRefused, match="taken"):
        await loser.connect(FORUM, "ann")


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


# Peer identity ----------------------------------------------------------


async def test_a_peer_comes_back_to_its_own_name_after_a_restart(radios: RadioFactory) -> None:
    """The failure this exists for: a session whose client restarted asks for the name it just had, and the hat
    still has it bound to a connection nobody is behind. Eviction is lazy -- it happens when a send to that
    connection fails -- so without this the owner is locked out of their own name until something happens to fail.

    The uid is not a secret and proves nothing. It prevents the accident; a determined session is not what any of
    this is for.
    """
    first = radios()
    was = await first.connect(FORUM, "ann")
    keeping_the_net_up = radios()
    await keeping_the_net_up.connect(FORUM, "watcher")

    returning = radios()
    again = await returning.connect(FORUM, "ann", peer_uid=was.peer_uid)
    assert again.name == "ann"
    assert again.connection_id != was.connection_id  # a new connection, the same participant

    stranger = radios()
    with pytest.raises(ConnectionRefused, match="name taken"):
        await stranger.connect(FORUM, "ann")


async def test_rejoining_in_the_same_process_returns_the_membership_it_already_holds(radios: RadioFactory) -> None:
    """Resuming is not the same as joining twice: the pair names a membership this process still holds, so the
    answer is that membership rather than a second connection under one identity."""
    backend = radios()
    first = await backend.connect(FORUM, "ann")
    again = await backend.connect(FORUM, "ann", peer_uid=first.peer_uid, peer_secret=first.peer_secret)
    assert (again.connection_id, again.peer_uid) == (first.connection_id, first.peer_uid)
    assert len(backend.memberships) == 1


async def test_resuming_with_the_wrong_secret_is_refused(radios: RadioFactory) -> None:
    """The gate is local and only ever local -- the hat is told nothing about secrets and could not check one."""
    backend = radios()
    first = await backend.connect(FORUM, "ann")
    with pytest.raises(NotConnected, match="peer_secret"):
        await backend.connect(FORUM, "ann", peer_uid=first.peer_uid, peer_secret="not the one")


async def test_every_membership_gets_its_own_pair(radios: RadioFactory) -> None:
    """One process can hold several, and they are different participants as far as anything else is concerned."""
    backend = radios()
    here = await backend.connect(FORUM, "ann")
    there = await backend.connect(OTHER, "ann")
    assert here.peer_uid != there.peer_uid
    assert here.peer_secret != there.peer_secret


# Peer secrets at the tool boundary --------------------------------------


@pytest.mark.parametrize("tool", ["send", "check_inbox", "peers"])
async def test_acting_as_a_peer_needs_that_peer_s_secret(endpoint: str, monkeypatch, tool: str) -> None:
    """`join_channel` hands back a pair, and the three tools that act *as* that participant want the secret back.

    It lives here rather than with the other tool-boundary rules because it holds a real `Backend`, and on Windows
    the module that drives subprocesses runs on the proactor loop, where pyzmq cannot work at all -- which is
    exactly what `conftest.py` splits the loops for. Putting it there hung the Windows job for fifty minutes.

    It buys no boundary the operating system lacks -- everything runs under one user account, and the secret sits
    in the transcript -- which is exactly why it is called an honour-system convention. What it prevents is the
    accident: one conversation reaching into another's connection in a client that runs a single server for the
    whole application, where connection ids are visible in every result.
    """
    backend = Backend(endpoint)
    monkeypatch.setattr(frontend, "_radio", backend)
    try:
        joined = await frontend.join_channel(None, "forum", "ann")
        assert len(joined["peer_uid"]) == 26
        assert len(joined["peer_secret"]) == 26
        assert joined["peer_secret"] != joined["peer_uid"]

        arguments: dict[str, Any] = {"connection_id": joined["connection_id"]}
        if tool == "send":
            arguments["body"] = "hi"
        call = getattr(frontend, tool)

        refused = await call(**arguments)
        assert "peer_secret" in refused["error"]
        assert "next_step" in refused  # a refusal that does not say what to do next is a dead end

        allowed = await call(**arguments, peer_secret=joined["peer_secret"])
        assert "error" not in allowed
    finally:
        await backend.disconnect_all()
        backend.close()


