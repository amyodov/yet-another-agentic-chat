"""Who is on this machine, asked of the one address everything here already agrees on.

A hook, a watcher, a supervisor in another language -- none of them can read a tool result, so none of them can
be handed anything. What they can do is what every participant does: connect to the rendezvous point and ask.
The answer is a directory: one entry per session, each with the address of its notice socket.

This is deliberately the only way in. An earlier version had outside processes compute the address from a name
the user wrote into two config files, which could not survive a client that keeps one configuration block for a
whole machine, and could not be reproduced in a language whose crypto library declines to truncate a digest.

Blocking and stdlib-plus-pyzmq on purpose: the caller is usually a hook that runs between turns, and every
import it makes is latency somebody waits through.
"""

import logging

import zmq

from . import protocol
from .backend import DEFAULT_ENDPOINT

logger = logging.getLogger(__name__)

TIMEOUT_MS = 1000
"""Long enough for a loopback round trip many times over, short enough that a net with nobody home costs a blink."""


def directory(endpoint: str = DEFAULT_ENDPOINT, timeout_ms: int = TIMEOUT_MS) -> list[dict]:
    """Every session that has announced itself, or an empty list when nobody answers.

    Nobody answering is the ordinary case -- no session has joined anything, so no session is relaying -- and it
    is reported as emptiness rather than as an error, because there is nothing for a caller to do about it.
    """
    context = zmq.Context()
    asking = context.socket(zmq.DEALER)
    asking.setsockopt(zmq.LINGER, 0)
    asking.setsockopt(zmq.RCVTIMEO, timeout_ms)
    asking.setsockopt(zmq.ROUTING_ID, protocol.new_ulid().encode("ascii"))
    answer = None
    try:
        asking.connect(endpoint)
        # `sessions` first, then a question every version has ever answered. A hat too old to know `sessions`
        # logs it and says nothing, so a lone query would wait out the whole timeout on every hook event, for as
        # long as that peer keeps the hat. A ROUTER handles one peer's messages in order, so a `channels` reply
        # arriving with no `sessions` answer ahead of it proves the peer read the query and had nothing to say.
        asking.send(protocol.dumps(protocol.sessions_query().to_wire()))
        asking.send(protocol.dumps(protocol.channels_query().to_wire()))
        while True:
            heard = protocol.Envelope.from_wire(protocol.parse(asking.recv()))
            if heard.op == "sessions":
                answer = heard
                break
            if heard.op == "channels":
                logger.info("%s answers channels but not sessions; too old for the directory", endpoint)
                return []
    except (zmq.ZMQError, ValueError) as exc:
        logger.info("no answer from %s: %s", endpoint, exc)
        return []
    finally:
        asking.close()
        context.term()

    if not isinstance(answer.payload, dict):
        return []
    listed = answer.payload.get("sessions")
    return listed if isinstance(listed, list) else []
