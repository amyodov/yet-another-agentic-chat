"""Routing failures must reach the sender rather than disappear in the relay."""

import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest
import zmq
import zmq.asyncio

from yaac import protocol
from yaac.hat import ChannelInfo, Hat
from yaac.protocol import Address, Identity, Scope


@pytest.mark.parametrize("recipient", [None, Address(name="bob")], ids=["broadcast", "direct"])
def test_unknown_sender_is_held_and_delivered_after_hello(recipient: Address | None) -> None:
    hat = Hat(Mock())
    sent = []
    hat._send = lambda routing_id, message: sent.append((routing_id, message)) or True
    hat._hello(b"bob-id", {"channel": "forum", "name": "bob"})
    sent.clear()
    message = protocol.message(Scope(channel="forum", peer=recipient), body="after changeover", msg_id="held-id")

    hat._relay(b"ann-id", message)
    assert [(rid, envelope.op) for rid, envelope in sent] == [(b"ann-id", "whois")]
    assert [held.envelope for held in hat.pending[b"ann-id"]] == [message]

    hat._hello(b"ann-id", {"channel": "forum", "name": "ann"})
    carried = [(rid, envelope) for rid, envelope in sent if envelope.id == "held-id"]
    assert carried == [
        (
            b"bob-id",
            replace(
                message,
                to=Scope(channel="forum", peer=Address(name="bob", routing_id="bob-id") if recipient else None),
                frm=Scope(channel="forum", peer=Address(name="ann", routing_id="ann-id")),
            ),
        )
    ]
    assert (hat.pending, hat.whois_inflight) == ({}, set())


@pytest.fixture
async def router():
    context = zmq.asyncio.Context()
    socket = context.socket(zmq.ROUTER)
    socket.setsockopt(zmq.ROUTER_MANDATORY, 1)
    port = socket.bind_to_random_port("tcp://127.0.0.1")
    yield socket, context, f"tcp://127.0.0.1:{port}"
    socket.close(linger=0)
    context.term()


@pytest.mark.parametrize("operation", ["send", "reachable", "relay", "reclaim", "connected", "unexpected-error"])
async def test_unreachable_async_router_is_detected_before_return(
    router, operation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket, context, endpoint = router
    hat = Hat(socket)
    dealer = context.socket(zmq.DEALER)
    dealer.setsockopt(zmq.ROUTING_ID, b"ann-id")
    dealer.connect(endpoint)
    try:
        await dealer.send(b"connected")
        assert await socket.recv_multipart() == [b"ann-id", b"connected"]
        # A stale table entry whose transport has disappeared: libzmq knows no bob-id.
        hat.routing_ids = {b"ann-id": Identity("forum", "ann"), b"bob-id": Identity("forum", "bob")}
        hat.by_name = {("forum", "ann"): b"ann-id", ("forum", "bob"): b"bob-id"}
        hat.channels = {"forum": ChannelInfo("forum", members={b"ann-id", b"bob-id"})}
        match operation:
            case "connected":
                message = protocol.whois(to=Scope(peer=Address(routing_id="ann-id")))
                assert hat._send(b"ann-id", message) is True
                assert await asyncio.wait_for(dealer.recv(), 1) == protocol.dumps(message.to_wire())
                assert hat.routing_ids[b"bob-id"] == Identity("forum", "bob")
                return
            case "unexpected-error":
                failing = Mock()
                failing.send_multipart.side_effect = zmq.ZMQError(zmq.EINVAL)
                monkeypatch.setattr(zmq.Socket, "shadow", lambda router: failing)
                with pytest.raises(zmq.ZMQError) as raised:
                    hat._reachable(b"bob-id")
                assert raised.value.errno == zmq.EINVAL
                assert hat.routing_ids[b"bob-id"] == Identity("forum", "bob")
                return
            case "send":
                assert hat._send(b"bob-id", protocol.whois(to=Scope(peer=Address(routing_id="bob-id")))) is False
            case "reachable":
                assert hat._reachable(b"bob-id") is False
            case "relay":
                hat._relay(b"ann-id", protocol.message(Scope(peer=Address(name="bob")), body="hello", msg_id="lost-id"))
                received = []
                # Eviction sends an updated roster before the directed-message bounce.
                for _ in range(2):
                    raw = await asyncio.wait_for(dealer.recv(), 1)
                    received.append(protocol.Envelope.from_wire(protocol.parse(raw)))
                assert [(item.op, item.payload) for item in received if item.op == "bounce"] == [
                    ("bounce", {"id": "lost-id", "reason": "recipient went away"})
                ]
            case "reclaim":
                hat._hello(b"ann-id", {"channel": "forum", "name": "bob"})
                assert hat.by_name == {("forum", "bob"): b"ann-id"}
                assert hat.routing_ids == {b"ann-id": Identity("forum", "bob")}
        assert b"bob-id" not in hat.routing_ids
    finally:
        dealer.close(linger=0)
