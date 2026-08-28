"""The rules that are error conditions rather than behaviour.

These assert properties the design would be broken without, and each one exists
because getting it wrong is either invisible or catastrophic in production.
"""

import asyncio
import contextlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVER = [sys.executable, "-c", "from yaac.frontend import main; main()"]


async def run_server(endpoint: str, requests: list[dict], env: dict[str, str] | None = None) -> tuple[bytes, bytes]:
    """Drive the MCP server over stdio and return its raw stdout and stderr.

    stdin is deliberately held open until the last reply has been read: EOF ends
    the stdio loop, and closing early races it into shutting down before it has
    answered. What EOF does after that is its own test, below.
    """
    process = await asyncio.create_subprocess_exec(
        *SERVER,
        "--endpoint",
        endpoint,
        cwd=REPO,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr

    lines: list[bytes] = []

    async def exchange() -> None:
        """Send each request only after the previous reply arrived.

        The server routing_ids requests concurrently, so pipelining them lets a tools/list overtake the tools/call that
        was supposed to change the tool list.
        """
        for request in requests:
            process.stdin.write((json.dumps(request) + "\n").encode())
            await process.stdin.drain()
            if "id" not in request:
                continue
            while line := await process.stdout.readline():
                lines.append(line)
                if json.loads(line).get("id") == request["id"]:
                    break

    try:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(exchange(), timeout=30)
    finally:
        process.kill()
        await process.wait()
        stderr = await process.stderr.read()
    return b"".join(lines), stderr


def decode(stdout: bytes) -> list[dict]:
    """Every non-blank line of the server's stdout, parsed."""
    return [json.loads(line) for line in stdout.decode().splitlines() if line.strip()]


def handshake(client_name: str = "test") -> list[dict]:
    """Initialize, confirm, and list -- as the named client. The name decides whether the tool list may change."""
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]


HANDSHAKE = handshake()
DORMANT_TOOLS = {"list_channels", "join_channel"}
EVERY_TOOL = DORMANT_TOOLS | {"send", "check_inbox", "peers", "leave_channel", "dev_connections"}


async def test_startup_writes_only_json_rpc_to_stdout(endpoint: str) -> None:
    """stdout is the MCP stdio transport. One stray print kills the session with
    an opaque parse error, so every line there must be valid JSON-RPC."""
    stdout, stderr = await run_server(endpoint, HANDSHAKE)

    # decode() raises if anything non-JSON leaked onto the stream.
    assert {message.get("jsonrpc") for message in decode(stdout)} == {"2.0"}
    # Logging must still happen -- just on the other stream.
    assert b"[yaac]" in stderr


async def test_the_server_never_writes_a_file_dormant_or_on_air(endpoint: str, tmp_path: Path) -> None:
    """YAAC keeps everything in memory. Nothing it holds should outlive the process, however the process ends, so
    there is nothing to clean up after a crash and nothing to leak between sessions."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    requests = HANDSHAKE + [
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "peers", "arguments": {}}},
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "join_channel", "arguments": {"channel": "forum", "name": "ann"}},
        },
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "dev_connections", "arguments": {}}},
    ]
    # The parent environment is inherited, not replaced: a replacement env without SYSTEMROOT cannot even start
    # Python on Windows. What the test needs is only that every location the server might treat as writable --
    # home and temp, under both the POSIX and the Windows variable names -- points into the watched directory.
    redirect = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "TMPDIR": str(runtime),
        "TMP": str(runtime),
        "TEMP": str(runtime),
        "XDG_RUNTIME_DIR": str(runtime),
        "PYTHONPATH": str(REPO / "src"),
    }
    stdout, _ = await run_server(endpoint, requests, env={**os.environ, **redirect})
    # The server really did run, go on air, and answer -- and still wrote nothing anywhere it could reach.
    assert [m["id"] for m in decode(stdout) if "id" in m] == [1, 2, 3, 4, 5]
    assert list(runtime.rglob("*")) == []
    assert list(runtime.iterdir()) == []


@pytest.mark.parametrize(
    "client_name,expected",
    [("test", DORMANT_TOOLS), ("codex-mcp-client", EVERY_TOOL)],
    ids=["acts-on-the-notification", "ignores-it"],
)
async def test_the_dormant_tool_surface_is_what_this_client_can_act_on(
    endpoint: str, client_name: str, expected: set[str]
) -> None:
    """A dormant server runs in every session the user has, so its tool surface is the whole cost it imposes on the
    sessions that never join a channel. Paying that cost only buys something on a client that re-reads the list;
    on one that does not, a withheld tool is withheld for the whole session."""
    stdout, _ = await run_server(endpoint, handshake(client_name))
    [init] = [m["result"] for m in decode(stdout) if m.get("id") == 1]
    [tools] = [m["result"]["tools"] for m in decode(stdout) if m.get("id") == 2]

    # A client acts on notifications/tools/list_changed only if the server said its list can change. Advertising
    # false and then sending the notification is correctly ignored, which is what made an earlier version of this
    # code look as though the client were at fault.
    assert init["capabilities"]["tools"] == {"listChanged": True}
    assert {t["name"] for t in tools} == expected
    # Tool descriptions are read by a model on every session; an undescribed tool is one it will misuse.
    assert [t["name"] for t in tools if not t["description"].strip()] == []


@pytest.mark.parametrize("client_name", ["test", "codex-mcp-client"], ids=["relists", "does-not"])
async def test_the_hooks_tool_is_callable_but_never_offered(endpoint: str, client_name: str) -> None:
    """`hook_report` exists for Claude Code's `mcp_tool` hooks, which name the tool they call and do not need it
    listed. Listing it would put a second, differently-behaved `check_inbox` in front of the model on every
    session; unregistering it would make the hook fail on every tool call, in a session that has joined nothing as
    much as in one that has. So it is registered and withheld, in both states and for either kind of client."""
    join = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "join_channel", "arguments": {"channel": "forum", "name": "ann"}},
    }
    call = {
        "jsonrpc": "2.0",
        "id": 5,
        "method": "tools/call",
        "params": {"name": "hook_report", "arguments": {"event": "Stop"}},
    }
    listing = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
    stdout, _ = await run_server(endpoint, handshake(client_name) + [join, listing, call])
    messages = decode(stdout)

    for reply in (2, 4):  # dormant, then on air
        [tools] = [m["result"]["tools"] for m in messages if m.get("id") == reply]
        assert "hook_report" not in {t["name"] for t in tools}
    [answer] = [m["result"] for m in messages if m.get("id") == 5]
    # Nothing had been sent, so it declines to speak -- and says so in the hook's own vocabulary rather than by
    # failing, because a hook that errors reports an error on every tool call for the rest of the session.
    assert answer.get("isError") is not True
    assert json.loads(answer["content"][0]["text"]) == {"suppressOutput": True}


@pytest.mark.parametrize(
    "tool,expected",
    [
        ("list_channels", {"readOnlyHint": True, "openWorldHint": False}),
        ("peers", {"readOnlyHint": True, "openWorldHint": False}),
        ("dev_connections", {"readOnlyHint": True, "openWorldHint": False}),
        (
            "join_channel",
            {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
        ),
        ("send", {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}),
        (
            "check_inbox",
            {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        ),
        (
            "leave_channel",
            {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
        ),
    ],
)
async def test_each_tool_tells_the_client_what_calling_it_costs(
    endpoint: str, tool: str, expected: dict[str, bool]
) -> None:
    """A client decides from these whether a call needs the user's confirmation. read-only claims a call is a look:
    repeatable, safe to retry, safe to make speculatively. check_inbox is the one that would be tempting to call a
    look and must not be -- it takes the messages, so a speculative call consumes mail in a context that may have
    no way to act on it. The listing itself is asserted, not the constants, because the wire is what a client sees.
    """
    # codex-mcp-client gets all seven at connect, which is how the on-air tools reach a single tools/list here.
    stdout, _ = await run_server(endpoint, handshake("codex-mcp-client"))
    [tools] = [m["result"]["tools"] for m in decode(stdout) if m.get("id") == 2]
    [listed] = [t for t in tools if t["name"] == tool]
    assert listed["annotations"] == expected


@pytest.mark.parametrize(
    "client_name,after_leaving,notifications",
    [
        ("test", DORMANT_TOOLS, ["notifications/tools/list_changed"] * 2),
        ("codex-mcp-client", EVERY_TOOL, []),
    ],
    ids=["acts-on-the-notification", "ignores-it"],
)
async def test_the_tool_list_only_moves_for_a_client_that_would_notice(
    endpoint: str, client_name: str, after_leaving: set[str], notifications: list[str]
) -> None:
    """Going on air adds five tools and leaving takes them back, announced each way. For a client that never
    re-reads the list there is nothing to announce: it was given everything at launch and keeps it, so a
    notification would only claim a change the client cannot see and the withdrawal would be permanent."""
    connect = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "join_channel", "arguments": {"channel": "forum", "name": "ann"}},
    }
    listing = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
    leave = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "leave_channel", "arguments": {}}}
    final = {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}
    stdout, _ = await run_server(endpoint, handshake(client_name) + [connect, listing, leave, final])
    messages = decode(stdout)

    def listed(request_id):
        [tools] = [m["result"]["tools"] for m in messages if m.get("id") == request_id]
        return {t["name"] for t in tools}

    assert listed(4) == EVERY_TOOL
    assert listed(6) == after_leaving
    assert [m["method"] for m in messages if m.get("method")] == notifications


@pytest.mark.parametrize(
    "arguments,expect",
    [({}, "required"), ({"connection_id": "01NOSUCHCONNECTION"}, "no open connection")],
    ids=["omitted", "unknown"],
)
async def test_check_inbox_will_not_read_an_inbox_it_was_not_given(
    endpoint: str, arguments: dict[str, Any], expect: str
) -> None:
    """Reading empties the inbox, and one process serves every conversation in clients like Claude Desktop. A call
    that guessed, or accepted an id it does not hold, would consume mail belonging to another conversation."""
    join = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "join_channel", "arguments": {"channel": "forum", "name": "ann"}},
    }
    read = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {"name": "check_inbox", "arguments": arguments},
    }
    stdout, _ = await run_server(endpoint, HANDSHAKE + [join, read])
    [result] = [m for m in decode(stdout) if m.get("id") == 4]

    body = json.dumps(result).lower()
    assert expect in body
    # An omitted id fails schema validation; an unknown one is refused with the open connections listed, so a caller
    # that lost its id can recover rather than guess.
    if arguments:
        assert "open_connections" in body


async def test_a_server_whose_client_left_lets_go_of_the_port(endpoint: str) -> None:
    """A client going away is the ordinary end of every session, and the process has to actually end.

    It did not. `Backend.close` terminated the ZMQ context without closing the sockets in it, and termination
    waits for exactly that, so the process survived its client -- measured once at three days and eighteen hours,
    still holding the rendezvous port with its event loop long gone. Every session on the machine then found a
    port that accepts connections and answers nothing, which reads as an empty network rather than a broken one.

    The port is the assertion, not the exit code: a hat that cannot be replaced is the part that hurts other
    sessions.
    """
    port = int(endpoint.rsplit(":", 1)[1])
    process = await asyncio.create_subprocess_exec(
        *SERVER, "--endpoint", endpoint, cwd=REPO,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout

    for request in [*handshake(), {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                   "params": {"name": "join_channel",
                                              "arguments": {"channel": "forum", "name": "ann"}}}]:
        process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()
        if "id" in request:
            while line := await process.stdout.readline():
                if json.loads(line).get("id") == request["id"]:
                    break

    process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise AssertionError("the server outlived its client and kept the rendezvous port") from None

    with socket.socket() as probe:
        # SO_REUSEADDR because libzmq sets it, so this asks the question a successor would ask: connections the
        # departed hat had open leave sockets in TIME_WAIT, and those must not stand in the way.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))  # raises if the endpoint is still held


def test_no_python_file_reads_or_writes_text_without_naming_the_encoding() -> None:
    """Windows defaults to cp1252, not UTF-8, so reading a file without naming an encoding decodes bytes it cannot
    represent and dies -- which is how a README containing an em dash took a release-blocking CI run down. The
    failure is invisible on macOS and Linux, where the default is UTF-8, so only a rule catches it."""
    offenders = []
    for path in REPO.rglob("*.py"):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if ("read_text(" in line or "write_text(" in line) and "encoding=" not in line:
                offenders.append(f"{path.relative_to(REPO)}:{number}")
    assert offenders == []
