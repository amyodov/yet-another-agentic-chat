"""The MCP server: tool definitions.

Each tool is a single call that delegates to `Backend` and keeps no state of its own; `_radio` is the only
module-level state and exists so the backend can be constructed lazily.

All six tools are listed in every state, including dormant. Exposing only `list_channels` and `connect_to_channel`
until connected would require `notifications/tools/list_changed`; that was tested against Claude Code and the added
tool never became callable, in the same turn or in a later turn of the same session. The four on-air tools therefore
always exist and return a `not_connected` result until `connect_to_channel` succeeds.

Tool descriptions are read by the model on every session, so each is kept to one line plus the minimum context
needed to use it correctly.

"channel" here means a YAAC channel -- a named conversation between sessions. It is unrelated to the Claude Code
feature of the same name.
"""

import argparse
import asyncio
import sys
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .backend import (
    DEFAULT_ENDPOINT,
    Backend,
    ConnectionRefused,
    NotConnected,
    check_zmq_capabilities,
    log,
)

mcp = MCPServer(
    "yaac",
    instructions=(
        "YAAC is a radio between concurrently running agentic sessions. Sessions "
        "join a named channel under a nickname the user chooses, then talk to each "
        "other. Nothing is delivered on its own: while connected, call check_inbox "
        "before acting on anything, and again before you finish a turn, or messages "
        "from other sessions will sit unread."
    ),
)

# One Backend per server process, created on first use by radio(). While None, the process holds no sockets and has
# created no files.
_radio: Backend | None = None
_endpoint: str = DEFAULT_ENDPOINT


def radio() -> Backend:
    """Return the process's Backend, constructing it on first use so a server that never connects stays inert."""
    global _radio
    if _radio is None:
        _radio = Backend(_endpoint)
    return _radio


def _inbox_hint() -> dict[str, Any]:
    """Unread-message count, merged into tool results.

    MCP defines no server-initiated message that reaches the model's context, so unread messages cannot be pushed
    into an idle session. Attaching the count to results the model is already reading is the only in-protocol way to
    signal that `check_inbox` is worth calling.
    """
    if (waiting := radio().pending_count()) > 0:
        return {
            "unread": waiting,
            "action_required": f"{waiting} unread message(s) -- call check_inbox now",
        }
    return {"unread": 0}


@mcp.tool()
async def list_channels() -> dict[str, Any]:
    """List channels currently on the air and how many participants each has.

    Has no side effects and does not join anything; safe to call at any time.
    Takes up to 10 seconds to report an empty network.
    """
    channels = await radio().probe_channels()
    if channels is None:
        return {
            "channels": [],
            "status": "nobody on the air",
            "detail": "No session is running YAAC right now. This is normal, not an error.",
        }
    return {"channels": channels, "status": "on the air"}


@mcp.tool()
async def connect_to_channel(
    channel: Annotated[str, Field(description="Exact channel name, as the user gave it.")],
    nickname: Annotated[str, Field(description="Exact nickname, as the user gave it.")],
) -> dict[str, Any]:
    """Go on air: join CHANNEL as NICKNAME, creating the channel if it is empty.

    Ask the user to confirm both the channel and the nickname before calling this.
    Never invent a nickname or infer one from the directory, hostname, or task.
    """
    try:
        result = await radio().connect(channel, nickname)
    except ConnectionRefused as exc:
        return {
            "joined": False,
            "error": str(exc),
            "next_step": "Ask the user for a different nickname; do not choose one yourself.",
        }

    response: dict[str, Any] = {
        "joined": result.channel,
        "nickname": result.nickname,
        "created": result.created,
        "peers": result.peers,
        "reminder": (
            "Nothing arrives on its own. Call check_inbox before acting on anything and again before ending your turn."
        ),
    }
    if result.created:
        # A mistyped channel name silently produces a new empty channel, which is indistinguishable from a correct
        # one until nobody replies. Reporting creation is what makes that case detectable.
        response["confirm_with_user"] = (
            f"Nobody was here, so this created the channel {channel!r}. "
            f"Check with the user that this is the name they meant."
        )
    return response


@mcp.tool()
async def send(
    body: Annotated[str, Field(description="The message text.")],
    nickname: Annotated[
        str | None,
        Field(description="Recipient's nickname. Omit only to announce to everyone."),
    ] = None,
) -> dict[str, Any]:
    """Send a message to one participant, or to the whole channel if NICKNAME is omitted.

    Prefer addressing one person: a broadcast interrupts every session on the
    channel, so reserve it for genuine announcements. Returns "accepted", which
    means handed to the network -- not that anybody has read it.
    """
    try:
        message_id = await radio().send(body, nickname)
    except NotConnected as exc:
        return {"status": "not_connected", "error": str(exc)}
    except RuntimeError as exc:
        return {"status": "rejected", "error": str(exc)}
    return {
        "status": "accepted",
        "id": message_id,
        "to": nickname or "everyone on the channel",
        **_inbox_hint(),
    }


@mcp.tool()
async def check_inbox() -> dict[str, Any]:
    """Read everything other sessions have sent since you last checked.

    Call this whenever you are connected: before acting on anything, and again
    before ending your turn. Messages are never delivered on their own, so
    anything you do not collect here is simply not seen.
    """
    try:
        messages = radio().receive()
    except NotConnected as exc:
        return {"messages": [], "status": "not_connected", "error": str(exc)}
    return {
        "messages": messages,
        "count": len(messages),
        "status": "no new messages" if not messages else f"{len(messages)} new",
    }


@mcp.tool()
async def peers() -> dict[str, Any]:
    """List the nicknames currently on your channel, besides your own."""
    try:
        others = radio().peers()
    except NotConnected as exc:
        return {"peers": [], "status": "not_connected", "error": str(exc)}
    return {"peers": others, "count": len(others), **_inbox_hint()}


@mcp.tool()
async def disconnect() -> dict[str, Any]:
    """Leave the channel and go off air, removing this session's inbox."""
    if not radio().on_air:
        return {"status": "not_connected"}
    channel, nickname = radio().channel, radio().nickname
    await radio().disconnect()
    return {"status": "disconnected", "was": {"channel": channel, "nickname": nickname}}


def main() -> None:
    """Console entry point. Parses arguments, checks pyzmq, and serves MCP over stdio. Writes nothing to stdout."""
    global _endpoint

    parser = argparse.ArgumentParser(
        prog="yaac",
        description="A radio for agentic sessions. Run as an MCP server over stdio.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=(
            "Rendezvous endpoint (default: %(default)s). Its real purpose is letting "
            "tests run an isolated instance; nobody should need to set it."
        ),
    )
    args = parser.parse_args()
    _endpoint = args.endpoint

    try:
        check_zmq_capabilities()
    except RuntimeError as exc:
        print(f"[yaac] {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc

    log(f"dormant (rendezvous {_endpoint}); no sockets open, no files created")
    try:
        asyncio.run(mcp.run_stdio_async())
    except KeyboardInterrupt:
        pass
    finally:
        if _radio is not None:
            _radio.close()


if __name__ == "__main__":
    main()
