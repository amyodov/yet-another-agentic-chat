"""Waking a Codex session that is sitting idle.

A hook only fires when a session does something, so it cannot reach one that is waiting at its prompt. Codex can
be reached, though not by delivering: `turn/start` on its app-server begins a turn, which is the programmatic
equivalent of the user typing. Everything else follows from there -- hooks fire, and the model reads its history.

So this is an alarm clock rather than a postman. It says that mail is waiting and lets `check_inbox` do the
reading, which is the same division the notice socket already makes, for the same reason: what a session receives
should be what it chose to collect.

Two things keep it modest. It is off unless the user turns it on, because starting a turn spends tokens and runs
tools in somebody's session and that is not a decision a library should take unasked. And it names no `model`,
`cwd` or `approvalPolicy` -- `turn/start` requires only the thread and the input, so Codex answers those from the
thread's own configuration rather than from our guess about it.

The transport needs no discovery: `codex app-server proxy` connects to the running daemon's control socket and
speaks JSON-RPC over stdio, so this is a subprocess rather than a hunt for a socket path. A session that is not
running under that daemon has no thread to find, the call fails, and nothing happens -- the same shape as a
notice nobody is watching.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

WAKE_ENV = "YAAC_WAKE"
"""Set to anything non-empty to let this session be woken. Off by default, deliberately."""

PROXY = ("codex", "app-server", "proxy")
TIMEOUT_SECONDS = 15.0


def wanted() -> bool:
    """Whether this session asked to be woken. One env var, no configuration file, no negotiation."""
    return bool(os.environ.get(WAKE_ENV))


async def wake(thread: str, text: str, timeout: float = TIMEOUT_SECONDS) -> bool:
    """Start a turn in `thread` with `text` as its input. True when the app-server accepted it.

    Every failure is quiet and false: no daemon running, no such thread, `codex` not on PATH, a turn already in
    flight. The mail is in the inbox either way, and a session that cannot be woken is exactly a session that
    reads its mail the next time it does something.
    """
    request = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "turn/start",
            "params": {"threadId": thread, "input": [{"type": "text", "text": text}]},
        }
    )
    try:
        proxy = await asyncio.create_subprocess_exec(
            *PROXY,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.info("cannot reach the app-server to wake %s: %s", thread, exc)
        return False

    try:
        stdout, _ = await asyncio.wait_for(proxy.communicate((request + "\n").encode()), timeout=timeout)
    except TimeoutError:
        proxy.kill()
        await proxy.wait()
        logger.info("the app-server did not answer within %.0fs; leaving %s asleep", timeout, thread)
        return False

    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            answer = json.loads(line)
        except ValueError:
            continue
        if answer.get("id") != 1:
            continue
        if error := answer.get("error"):
            logger.info("the app-server refused to wake %s: %s", thread, error)
            return False
        return True
    return False
