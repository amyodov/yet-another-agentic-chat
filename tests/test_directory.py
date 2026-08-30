"""Asking the rendezvous point who is running here.

Synchronous throughout, and deliberately so: `directory` is what a hook calls between turns, in a plain process
with no event loop, and the fakes here are sync ROUTERs for the same reason. Nothing in this module holds an
asyncio Backend, so the Windows loop split in `conftest.py` does not apply to it.
"""

import threading
import time

import pytest
import zmq

from yaac import protocol
from yaac.directory import directory


class Answering:
    """A ROUTER at a rendezvous endpoint that replies to some operator mail and ignores the rest.

    `answers` names the ops it knows. A hat older than an op does exactly this -- logs it and says nothing -- so
    leaving `sessions` out of the set is a faithful 0.5.0 hat rather than an approximation of one.
    """

    def __init__(self, endpoint: str, answers: frozenset[str]) -> None:
        self.endpoint = endpoint
        self.answers = answers
        self.heard: list[str] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> Answering:
        self._thread.start()
        # The bind happens on the thread, so give it the moment it needs before a client connects.
        time.sleep(0.2)
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        context = zmq.Context()
        router = context.socket(zmq.ROUTER)
        router.setsockopt(zmq.LINGER, 0)
        router.setsockopt(zmq.RCVTIMEO, 100)
        router.bind(self.endpoint)
        try:
            while not self._stop.is_set():
                try:
                    source, raw = router.recv_multipart()
                except zmq.Again:
                    continue
                asked = protocol.Envelope.from_wire(protocol.parse(raw))
                self.heard.append(asked.op or "")
                to = protocol.Scope(peer=protocol.Address(routing_id=source.decode()))
                match asked.op:
                    case "sessions" if "sessions" in self.answers:
                        reply = protocol.sessions_answer([{"pid": 42, "cwd": "/here"}], to=to)
                    case "channels" if "channels" in self.answers:
                        reply = protocol.channels([], to=to)
                    case _:
                        continue
                router.send_multipart([source, protocol.dumps(reply.to_wire())])
        finally:
            router.close()
            context.term()


def test_a_hat_that_knows_the_directory_answers_it(endpoint: str) -> None:
    with Answering(endpoint, frozenset({"sessions", "channels"})) as hat:
        assert directory(endpoint) == [{"pid": 42, "cwd": "/here"}]
    # Both went out, because the pairing cannot know in advance which kind of peer it is talking to.
    assert hat.heard == ["sessions", "channels"]


def test_a_hat_too_old_for_the_directory_is_found_out_at_once(endpoint: str) -> None:
    """The trap this pairing exists for. A hat that predates an op logs it and never replies, so a lone query
    waits out its whole timeout -- on every hook event, for as long as that peer keeps the hat. A ROUTER handles
    one peer's messages in order, so a `channels` reply with no `sessions` answer ahead of it is proof."""
    with Answering(endpoint, frozenset({"channels"})) as hat:
        started = time.monotonic()
        assert directory(endpoint, timeout_ms=3000) == []
        spent = time.monotonic() - started

    assert hat.heard == ["sessions", "channels"]
    # Well under the timeout it would otherwise have burned, and not merely under it by a hair.
    assert spent < 1.0


def test_nobody_home_is_an_empty_answer_not_an_error(endpoint: str) -> None:
    """No session has joined anything, so no session is relaying. There is nothing a caller can do about it, and
    the timeout is the only exit: ZMQ queues a message for a peer that is not there rather than failing."""
    started = time.monotonic()
    assert directory(endpoint, timeout_ms=300) == []
    assert time.monotonic() - started >= 0.3


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"sessions": [{"pid": 1}]}, [{"pid": 1}]),
        ({"sessions": []}, []),
        ({"sessions": "not a list"}, []),
        ({}, []),
    ],
    ids=["listed", "empty", "wrong-type", "absent"],
)
def test_a_malformed_answer_is_no_sessions_rather_than_a_crash(endpoint: str, payload: dict, expected: list) -> None:
    """A hook reporting nothing is a session that reads its mail a moment later; a hook raising is a failed hook
    on every tool call, which a user sees and YAAC does not."""

    def serve() -> None:
        context = zmq.Context()
        router = context.socket(zmq.ROUTER)
        router.setsockopt(zmq.LINGER, 0)
        router.setsockopt(zmq.RCVTIMEO, 3000)
        router.bind(endpoint)
        try:
            source, raw = router.recv_multipart()
            to = protocol.Scope(peer=protocol.Address(routing_id=source.decode()))
            answer = protocol.message(to, frm=protocol.Scope(), op="sessions", payload=payload)
            router.send_multipart([source, protocol.dumps(answer.to_wire())])
        except zmq.Again:
            pass
        finally:
            router.close()
            context.term()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    time.sleep(0.2)
    try:
        assert directory(endpoint, timeout_ms=3000) == expected
    finally:
        thread.join(timeout=5)
