"""Routing failures must reach the sender rather than disappear in the relay."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from yaac import protocol
from yaac.hat import Hat
from yaac.protocol import Address, Scope


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
