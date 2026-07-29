"""Two or more radios on one net: the behaviour v0 is defined by."""

import asyncio

import pytest

from yaac.backend import AmbiguousConnection, Backend, ConnectionRefused, NotConnected

FORUM = "forum"
OTHER = "other channel"
SETTLE = 0.4  # generous for loopback TCP, short enough to keep the suite quick


@pytest.fixture
async def radios(endpoint):
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
            (m.get("from") or {}).get("nickname"),
            (m.get("to") or {}).get("nickname") if m.get("to") else None,
            m.get("body"),
        )
        for m in backend.resolve(None).receive()
    ]


async def test_probing_reports_the_net_without_joining_it(radios):
    prober = radios()
    assert await prober.probe_channels(timeout=1.0) is None
    # Probing must never bind, or a session that merely looked would become the
    # hub and drop it a second later.
    assert [prober.is_hub, prober.on_air] == [False, False]

    occupant = radios()
    await occupant.connect(FORUM, "ann")
    found = await radios().probe_channels(timeout=3.0)
    assert [(c["name"], c["count"]) for c in found] == [(FORUM, 1)]

    # A channel exists only while occupied.
    await occupant.disconnect()
    await asyncio.sleep(SETTLE)
    assert await radios().probe_channels(timeout=1.0) is None


async def test_joining_reports_creation_and_populates_both_rosters(radios):
    first = await radios().connect(FORUM, "ann")
    assert (first.created, first.peers) == (True, [])

    b = radios()
    second = await b.connect(FORUM, "bob")
    assert (second.created, second.peers) == (False, ["ann"])

    await asyncio.sleep(SETTLE)
    assert b.resolve(None).peer_nicknames() == ["ann"]


@pytest.mark.parametrize(
    "recipient,expected_to,sender_hears",
    [("bob", "bob", []), (None, None, [])],
    ids=["direct", "broadcast"],
)
async def test_delivery_reaches_the_right_radios_and_not_the_sender(radios, recipient, expected_to, sender_hears):
    a, b, c = radios(), radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")
    await c.connect(FORUM, "cid")

    await a.resolve(None).send("hold your commits", nickname=recipient)

    assert await heard(b) == [("ann", expected_to, "hold your commits")]
    # A third party hears a broadcast but not a message addressed to someone else.
    assert await heard(c) == ([] if recipient else [("ann", None, "hold your commits")])
    assert await heard(a) == sender_hears


@pytest.mark.parametrize(
    "recipient,on_other_channel",
    [("nobody", False), ("bob", True)],
    ids=["absent-nickname", "not-on-my-channel"],
)
async def test_undeliverable_messages_bounce_to_the_sender(radios, recipient, on_other_channel):
    a = radios()
    await a.connect(FORUM, "ann")
    if on_other_channel:
        b = radios()
        await b.connect(OTHER, "bob")

    await a.resolve(None).send("anyone there?", nickname=recipient)
    await asyncio.sleep(SETTLE)

    [bounce] = a.resolve(None).receive()
    assert bounce["kind"] == "bounce"
    assert bounce["from"] is None
    assert "no such recipient" in bounce["reason"]
    if on_other_channel:
        # The hub takes the sender's channel from its own table, so speaking into
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
async def test_a_body_is_delivered_verbatim_and_never_obeyed(radios, body):
    # If the hub parsed bodies, these would be acted on instead of delivered,
    # and any participant could forge the control plane or another identity.
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")

    await a.resolve(None).send(body, nickname="bob")
    await asyncio.sleep(SETTLE)

    [received] = b.resolve(None).receive()
    assert received["body"] == body
    assert received["from"]["nickname"] == "ann"
    assert received["to"]["nickname"] == "bob"
    assert "kind" not in received


@pytest.mark.parametrize(
    "channel,nickname",
    [
        ("forum", "ann"),
        ("日本語", "名前"),
        ("café", "naïve"),
        ("channel with 🛰", "nick with 🛰"),
        ("  padded  ", "  padded  "),
        ('quotes "and" \\slashes', "null"),
        ("a" * 300, "b" * 300),
    ],
    ids=["ascii", "cjk", "accented", "emoji", "whitespace", "punctuation", "very-long"],
)
async def test_names_are_unrestricted_raw_text(radios, channel, nickname):
    # Transport constraints must not leak into names the user chose: routing uses
    # a separate opaque handle precisely so arbitrary text is safe here.
    a, b = radios(), radios()
    result = await a.connect(channel, nickname)
    assert (result.channel, result.nickname) == (channel, nickname)

    await b.connect(channel, "listener")
    await a.resolve(None).send("hello", nickname="listener")
    assert await heard(b) == [(nickname, "listener", "hello")]


async def test_a_taken_nickname_is_refused_rather_than_stolen(radios):
    await radios().connect(FORUM, "bob")

    with pytest.raises(ConnectionRefused, match="taken"):
        await radios().connect(FORUM, "bob")

    # Channels are addressing, not namespacing: the same name elsewhere is fine.
    elsewhere = await radios().connect(OTHER, "bob")
    assert elsewhere.created is True


async def test_losing_the_hub_restores_itself_with_no_user_action(radios):
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")
    assert [a.is_hub, b.is_hub] == [True, False]

    await a.disconnect()
    c = radios()
    await c.connect(FORUM, "cid")

    for _ in range(50):  # the election retries every ~2 s with jitter
        await asyncio.sleep(0.2)
        if b.is_hub or c.is_hub:
            break
    assert b.is_hub or c.is_hub  # somebody must have taken over the bind

    await asyncio.sleep(1.0)
    await c.resolve(None).send("still alive?", nickname="bob")
    assert await heard(b) == [("cid", "bob", "still alive?")]


async def test_leaving_returns_to_dormant_and_drops_what_was_held(radios):
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    await b.connect(FORUM, "bob")
    await b.resolve(None).send("unread when ann leaves", nickname="ann")
    await asyncio.sleep(SETTLE)
    assert a.resolve(None).pending_count() == 1

    await a.disconnect()
    assert [a.on_air, a.is_hub, a.memberships] == [False, False, {}]


@pytest.mark.parametrize("operation", ["send", "receive", "peers"])
async def test_on_air_operations_refuse_while_dormant(radios, operation):
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


async def test_connecting_twice_is_refused_and_a_refusal_leaves_no_trace(radios):
    radio = radios()
    await radio.connect(FORUM, "ann")
    with pytest.raises(ConnectionRefused, match="already on"):
        await radio.connect(FORUM, "ann")

    loser = radios()
    with pytest.raises(ConnectionRefused, match="taken"):
        await loser.connect(FORUM, "ann")
    # A refused join must leave the backend exactly as dormant as it was.
    assert [loser.on_air, loser.is_hub, loser.memberships] == [False, False, {}]


async def test_one_process_holds_several_memberships_independently(radios):
    """Clients that run one MCP server per application rather than per conversation would otherwise force every
    conversation to share a nickname and an inbox."""
    host, other = radios(), radios()
    first = await host.connect(FORUM, "ann")
    second = await host.connect(OTHER, "deputy")
    assert first.connection_id != second.connection_id
    assert [(c["channel"], c["nickname"]) for c in host.describe_all()] == [(FORUM, "ann"), (OTHER, "deputy")]

    # Each membership is an ordinary participant as far as the hub is concerned.
    await other.connect(OTHER, "bob")
    await host.resolve(second.connection_id).send("only deputy can send this", nickname="bob")
    assert await heard(other) == [("deputy", "bob", "only deputy can send this")]

    # And each has its own inbox.
    await other.resolve(None).send("for deputy only", nickname="deputy")
    await asyncio.sleep(SETTLE)
    assert [m["body"] for m in host.resolve(second.connection_id).receive()] == ["for deputy only"]
    assert host.resolve(first.connection_id).receive() == []


async def test_a_connection_id_is_required_only_when_it_is_ambiguous(radios):
    host = radios()
    first = await host.connect(FORUM, "ann")
    # One membership: omitting the id is unambiguous, so it resolves.
    assert host.resolve(None).nickname == "ann"

    await host.connect(OTHER, "deputy")
    with pytest.raises(AmbiguousConnection):
        host.resolve(None)
    assert host.resolve(first.connection_id).nickname == "ann"

    with pytest.raises(NotConnected):
        host.resolve("01NOSUCHCONNECTION")


async def test_the_bind_is_released_when_the_last_membership_goes(radios):
    """A process with no memberships must hold nothing, so another session can take the endpoint."""
    host = radios()
    first = await host.connect(FORUM, "ann")
    second = await host.connect(OTHER, "deputy")
    assert host.is_hub is True

    await host.disconnect(first.connection_id)
    assert host.is_hub is True  # still one membership open
    await host.disconnect(second.connection_id)
    assert [host.is_hub, host.on_air] == [False, False]


@pytest.mark.parametrize("locator", ["nickname", "handle"], ids=["by-nickname", "by-handle"])
async def test_a_recipient_can_be_named_by_either_locator(radios, locator):
    """A nickname is only unique while its holder is connected; a handle identifies one connection and is never
    reused. Both must reach the same participant."""
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    target = await b.connect(FORUM, "bob")

    keyword = {locator: "bob" if locator == "nickname" else target.connection_id}
    await a.resolve(None).send("addressed precisely", **keyword)
    assert await heard(b) == [("ann", "bob", "addressed precisely")]


async def test_a_handle_from_another_channel_does_not_resolve(radios):
    """Recipients are looked up within the sender's own channel, so a handle borrowed from elsewhere must bounce
    rather than deliver across the boundary."""
    a, b = radios(), radios()
    await a.connect(FORUM, "ann")
    elsewhere = await b.connect(OTHER, "bob")

    await a.resolve(None).send("wrong channel", handle=elsewhere.connection_id)
    await asyncio.sleep(SETTLE)
    assert await heard(b) == []
    [bounce] = a.resolve(None).receive()
    assert bounce["kind"] == "bounce"
