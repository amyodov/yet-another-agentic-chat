"""Delivering into the Claude Code session that started this server, through the inbox socket it exports.

Claude Code binds one inbox socket per session -- a Unix domain socket on macOS and Linux, a named pipe on
Windows -- for messages from the user's other sessions, and exports its path and a per-session token to every
child it starts as `CLAUDE_CODE_MESSAGING_SOCKET` and `CLAUDE_CODE_MESSAGING_TOKEN`. This server is such a child.
A line written there is a user message: it lands between tool calls while the session works and starts a new
turn while it sits idle, which is the one case neither a hook nor MCP can reach. Measured on 2026-09-08 against
Claude Code 2.1.261, from this very server process.

The wire, read out of the binary's own usage snippet and then measured: NDJSON, first
`{"type":"auth","token":…}`, then `{"type":"user","message":{"role":"user","content":…}}` -- the Agent SDK's
stream-json user message. The socket answers nothing, and drops an unknown type without a word, which is how the
first three guesses at the shape vanished. A top-level `from` is ignored: the receiver frames the text as "another
Claude session sent a message" and names no sender, so who wrote it goes in the content. The token is sent
always: optional on macOS and Linux, required on Windows, and the only thing that verifies a child after it has
exited or in a container where Claude Code is pid 1. The connection is opened only once the line is ready,
because the receiver closes one that has not completed a line within 30 s.

This is a postman rather than an alarm clock, unlike the Codex wake. The post carries the messages themselves,
because text placed in the model's context is a delivery whatever it is called, and a doorbell here would cost a
turn and then a `check_inbox` on top. The mail is taken from the inbox only once the post has gone through, so a
door that is gone loses nothing; and a hook that fires afterwards finds nothing, which is what stops the same text
arriving twice.

Nothing is configured: a session started by a Claude Code new enough exports the variables, and one that is not
has no door. Every failure is quiet -- a socket that is gone, a refused connection, a receiver that holds the
message for approval -- and the mail stays in the inbox for the hook or the model to collect.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

SOCKET_ENV = "CLAUDE_CODE_MESSAGING_SOCKET"
TOKEN_ENV = "CLAUDE_CODE_MESSAGING_TOKEN"
TIMEOUT_SECONDS = 5.0
PIPE_PREFIX = "\\\\.\\pipe\\"
"""How Windows spells a named pipe. asyncio has no client transport for one, so it is written from a thread."""


def door() -> tuple[str, str] | None:
    """Where this session's inbox socket is and the token it wants, or None when Claude Code did not export them.

    Only the path decides: a Claude Code too old for messaging exports neither, and no other client exports either.
    """
    if path := os.environ.get(SOCKET_ENV):
        return path, os.environ.get(TOKEN_ENV, "")
    return None


def lines(text: str, token: str) -> bytes:
    """The two lines a post is made of, in the shape the receiver reads: auth first, then a user message."""
    auth = json.dumps({"type": "auth", "token": token})
    user = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)
    return f"{auth}\n{user}\n".encode()


def _write_pipe(path: str, payload: bytes) -> None:
    with open(path, "wb", buffering=0) as pipe:
        pipe.write(payload)


async def post(text: str, path: str | None = None, token: str = "", timeout: float = TIMEOUT_SECONDS) -> bool:
    """Put `text` in front of the session as a user message. True when the socket took the whole line.

    The receiver acknowledges nothing, so "took" means the connection accepted the bytes: a socket nobody is
    listening on refuses the connect, and that is the failure this can see. What the receiver then does with the
    message -- delivers it, or holds it for the user's approval under a mode that requires it -- is its own.
    """
    if path is None:
        if (found := door()) is None:
            return False
        path, token = found
    payload = lines(text, token)
    try:
        if path.startswith(PIPE_PREFIX):
            await asyncio.wait_for(asyncio.to_thread(_write_pipe, path, payload), timeout=timeout)
        else:
            _, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout=timeout)
            try:
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), timeout=timeout)
            finally:
                writer.close()
    except (OSError, TimeoutError) as exc:
        logger.info("could not post to the session's inbox socket at %s: %s", path, exc)
        return False
    return True
