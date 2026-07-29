"""The MCP server: tool definitions.

Each tool is a single call that delegates to `Backend` and keeps no state of its own. `_radio` is the only
module-level state, so the backend can be constructed lazily.

The listed tools change with state. A dormant session shows only `list_channels` and `join_channel`; the rest
are added when the first membership opens and removed when the last one closes. This needs
`notifications/tools/list_changed`, which Claude Code honours -- but only if the server advertises
`tools.listChanged: true` in its initialize response. The SDK's `run_stdio_async()` builds initialization options
with `NotificationOptions()`, every flag false, and offers no way to override them, so `main` runs the low-level
server directly. A server that advertises false and then sends the notification is correctly ignored.

Tool descriptions are read by the model on every session, so each is kept to one line plus the minimum context
needed to use it correctly.

"channel" here means a YAAC channel -- a named conversation between sessions. It is unrelated to the Claude Code
feature of the same name.
"""

import argparse
import asyncio
import contextlib
import sys
from typing import Annotated, Any

import mcp.server.stdio as stdio
from mcp.server.lowlevel.server import NotificationOptions
from mcp.server.mcpserver import Context, MCPServer
from pydantic import Field

from .backend import (
    DEFAULT_ENDPOINT,
    AmbiguousConnection,
    Backend,
    ConnectionRefused,
    Membership,
    NotConnected,
    check_zmq_capabilities,
    log,
)

mcp = MCPServer(
    "yaac",
    instructions=(
        "YAAC is a radio between concurrently running agentic sessions. Sessions join a named channel under a "
        "name the user chooses, then talk to each other. Nothing is delivered on its own: while connected, call "
        "check_inbox before acting on anything, and again before you finish a turn, or messages from other sessions "
        "will sit unread."
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


def _refused(exc: Exception) -> dict[str, Any]:
    """Turn a backend exception into a result the model can act on rather than a protocol error.

    Whatever went wrong, the open connections are listed: the caller's next move is always to name one, and it may
    have lost the id from its own context.
    """
    status = "ambiguous_connection" if isinstance(exc, AmbiguousConnection) else "not_connected"
    refusal: dict[str, Any] = {"status": status, "error": str(exc)}
    if open_connections := radio().describe_all():
        refusal["open_connections"] = open_connections
        refusal["next_step"] = "Call again with connection_id set to the one you mean."
    return refusal


def _unread(membership: Membership) -> dict[str, Any]:
    """Unread count for one connection, merged into that connection's tool results.

    Counted per connection, never per session. A count covering the whole process would tell a caller it has mail
    that check_inbox on its own connection cannot find, since the messages belong to a different one -- and one
    process serves every conversation in clients like Claude Desktop.

    MCP defines no server-initiated message that reaches the model's context, so unread messages cannot be pushed
    into an idle session. Attaching the count to results the model is already reading is the only in-protocol way to
    signal that check_inbox is worth calling.
    """
    result: dict[str, Any] = {"unread": membership.pending_count()}
    if result["unread"]:
        result["action_required"] = f"{result['unread']} unread on this connection -- call check_inbox now"

    # Mail on a connection other than this one is reported with its id, so it is addressable rather than an alarm
    # the caller cannot act on.
    elsewhere = [c for c in radio().describe_all() if c["unread"] and c["connection_id"] != membership.routing_id]
    if elsewhere:
        result["unread_on_other_connections"] = elsewhere
    return result


# -- always listed -------------------------------------------------------


@mcp.tool()
async def list_channels() -> dict[str, Any]:
    """List channels currently on the air and how many participants each has.

    Has no side effects and does not join anything; safe to call at any time. Takes up to 10 seconds to report an
    empty network.
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
async def join_channel(
    ctx: Context,
    channel: Annotated[str, Field(description="Exact channel name, as the user gave it.")],
    name: Annotated[str, Field(description="Exact name, as the user gave it.")],
) -> dict[str, Any]:
    """Go on air: join CHANNEL as NAME. If nobody is on it, joining is what brings the channel into being.

    Ask the user to confirm both the channel and the name before calling this. Never invent a name or infer
    one from the directory, hostname, or task. Adds send, check_inbox, peers and leave_channel.
    """
    try:
        result = await radio().connect(channel, name)
    except ConnectionRefused as exc:
        return {
            "joined": False,
            "error": str(exc),
            "next_step": "Ask the user for a different name; do not choose one yourself.",
        }

    if len(radio().memberships) == 1:  # notify only when the tool set actually changes
        await _publish_on_air_tools(ctx)
    response: dict[str, Any] = {
        "joined": result.channel,
        "name": result.name,
        "connection_id": result.connection_id,
        "created": result.created,
        "peers": result.peers,
        "reminder": (
            "Nothing arrives on its own. Call check_inbox before acting on anything and again before ending your turn."
        ),
        # Published by notification, so the client may need a moment to re-fetch tools/list. Saying they exist
        # outright invites a call that fails while the list is still the old one.
        "new_tools": (
            "send, check_inbox, peers and leave_channel are being published now. If they are not "
            "listed yet, look again before assuming they are missing."
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


# -- listed only while on air --------------------------------------------

CONNECTION_ID = Annotated[
    str | None,
    Field(description="Which connection to use. Omit unless this session holds more than one."),
]


async def send(
    body: Annotated[str, Field(description="The message text.")],
    name: Annotated[str | None, Field(description="Recipient's name. Omit only to announce to everyone.")] = None,
    connection_id: CONNECTION_ID = None,
) -> dict[str, Any]:
    """Send a message to one participant, or to the whole channel if NAME is omitted.

    Prefer addressing one person: a broadcast interrupts every session on the channel, so reserve it for genuine
    announcements. Returns "accepted", which means handed to the network -- not that anybody has read it.
    """
    try:
        membership = radio().resolve(connection_id)
        message_id = await membership.send(body, name)
    except (NotConnected, AmbiguousConnection) as exc:
        return _refused(exc)
    except RuntimeError as exc:
        return {"status": "rejected", "error": str(exc)}
    return {
        "status": "accepted",
        "id": message_id,
        "from": membership.name,
        "to": name or "everyone on the channel",
        "connection_id": membership.routing_id,
        **_unread(membership),
    }


async def check_inbox(
    connection_id: Annotated[str, Field(description="The connection id join_channel gave you. Read only your own.")],
) -> dict[str, Any]:
    """Collect everything sent to CONNECTION_ID since you last checked.

    Call this whenever you are on a channel: before acting on anything, and again before ending your turn. Messages
    are never delivered on their own, so anything you do not collect here is simply not seen.

    The id is required, and reading removes the messages from that connection. One process serves every conversation
    in some clients, so a call that guessed could consume mail belonging to a different conversation.
    """
    try:
        membership = radio().resolve(connection_id)
    except (NotConnected, AmbiguousConnection) as exc:
        return _refused(exc)

    messages = membership.receive()
    return {
        "messages": messages,
        "count": len(messages),
        "channel": membership.channel,
        "as": membership.name,
        "status": f"{len(messages)} new" if messages else "no new messages",
        **_unread(membership),
    }


async def peers(connection_id: CONNECTION_ID = None) -> dict[str, Any]:
    """List the names currently on your channel, besides your own."""
    try:
        membership = radio().resolve(connection_id)
    except (NotConnected, AmbiguousConnection) as exc:
        return _refused(exc)
    others = membership.peers()
    return {
        "peers": [p.name for p in others],
        "addresses": [p.to_wire() for p in others],
        "count": len(others),
        "channel": membership.channel,
        **_unread(membership),
    }


async def leave_channel(ctx: Context, connection_id: CONNECTION_ID = None) -> dict[str, Any]:
    """Leave one channel and remove that connection's inbox. Any other channel you are on is unaffected."""
    try:
        membership = await radio().disconnect(connection_id)
    except (NotConnected, AmbiguousConnection) as exc:
        return _refused(exc)
    if not radio().on_air:
        await _withdraw_on_air_tools(ctx)
    return {
        "status": "left",
        "was": {"channel": membership.channel, "name": membership.name},
        "still_joined": radio().describe_all(),
    }


async def dev_connections() -> dict[str, Any]:
    """Diagnostic: every connection this session holds, with ids, channels, names and unread counts.

    Not needed in normal use -- one connection needs no id, and a call that is ambiguous already reports the
    choices. Useful when inspecting what a session is actually holding.
    """
    open_connections = radio().describe_all()
    return {"connections": open_connections, "count": len(open_connections)}


ON_AIR_TOOLS = (send, check_inbox, peers, leave_channel, dev_connections)


async def _publish_on_air_tools(ctx: Context) -> None:
    """Add the on-air tools and tell the client its list changed."""
    for tool in ON_AIR_TOOLS:
        mcp.add_tool(tool)
    await _announce_tool_change(ctx)


async def _withdraw_on_air_tools(ctx: Context) -> None:
    """Remove the on-air tools once the last membership closes."""
    for tool in ON_AIR_TOOLS:
        with contextlib.suppress(Exception):
            mcp.remove_tool(tool.__name__)
    await _announce_tool_change(ctx)


async def _announce_tool_change(ctx: Context) -> None:
    """Tell the client to re-read tools/list.

    Suppressed on failure: a client that cannot receive this still has a working server, and the tool result says
    what to do when the new tools do not appear.
    """
    with contextlib.suppress(Exception):
        await ctx.session.send_tool_list_changed()


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
            "Rendezvous endpoint (default: %(default)s). Its real purpose is letting tests run an isolated "
            "instance; nobody should need to set it."
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
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
    finally:
        if _radio is not None:
            _radio.close()


async def _serve() -> None:
    """Run the low-level server so `tools.listChanged` can be advertised.

    `MCPServer.run_stdio_async()` would advertise it as false, and a client that is told the list never changes is
    right to ignore the notification when it does.
    """
    low = mcp._lowlevel_server
    options = low.create_initialization_options(NotificationOptions(tools_changed=True))
    async with stdio.stdio_server() as (read, write):
        await low.run(read, write, options)


if __name__ == "__main__":
    main()
