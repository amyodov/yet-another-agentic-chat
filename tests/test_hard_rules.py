"""The rules that are error conditions rather than behaviour.

These assert properties the design would be broken without, and each one exists
because getting it wrong is either invisible or catastrophic in production.
"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parent.parent
SERVER = [sys.executable, "-c", "from yaac.frontend import main; main()"]


async def run_server(endpoint: str, requests: list[dict], env: dict[str, str] | None = None) -> tuple[bytes, bytes]:
    """Drive the MCP server over stdio and return its raw stdout and stderr.

    stdin is deliberately held open until the last reply has been read: the
    server shuts down on EOF, and closing early races it into exiting before it
    has answered.
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


HANDSHAKE = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
]


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
    stdout, _ = await run_server(
        endpoint,
        requests,
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(runtime),
            "XDG_RUNTIME_DIR": str(runtime),
            "PYTHONPATH": str(REPO / "src"),
        },
    )
    # The server really did run, go on air, and answer -- and still wrote nothing anywhere it could reach.
    assert [m["id"] for m in decode(stdout) if "id" in m] == [1, 2, 3, 4, 5]
    assert list(runtime.rglob("*")) == []
    assert list(runtime.iterdir()) == []


async def test_a_dormant_session_lists_only_the_two_tools_it_can_honour(endpoint: str) -> None:
    """A dormant server runs in every session the user has, so its tool surface is the whole cost it imposes on the
    sessions that never join a channel."""
    stdout, _ = await run_server(endpoint, HANDSHAKE)
    [init] = [m["result"] for m in decode(stdout) if m.get("id") == 1]
    [tools] = [m["result"]["tools"] for m in decode(stdout) if m.get("id") == 2]

    # A client acts on notifications/tools/list_changed only if the server said its list can change. Advertising
    # false and then sending the notification is correctly ignored, which is what made an earlier version of this
    # code look as though the client were at fault.
    assert init["capabilities"]["tools"] == {"listChanged": True}
    assert {t["name"] for t in tools} == {"list_channels", "join_channel"}
    # Tool descriptions are read by a model on every session; an undescribed tool is one it will misuse.
    assert [t["name"] for t in tools if not t["description"].strip()] == []


async def test_connecting_publishes_the_on_air_tools_and_disconnecting_withdraws_them(endpoint: str) -> None:
    connect = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "join_channel", "arguments": {"channel": "forum", "name": "ann"}},
    }
    listing = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
    leave = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "leave_channel", "arguments": {}}}
    final = {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}
    stdout, _ = await run_server(endpoint, HANDSHAKE + [connect, listing, leave, final])
    messages = decode(stdout)

    def listed(request_id):
        [tools] = [m["result"]["tools"] for m in messages if m.get("id") == request_id]
        return {t["name"] for t in tools}

    on_air = {"list_channels", "join_channel", "send", "check_inbox", "peers", "leave_channel", "dev_connections"}
    assert listed(4) == on_air
    assert listed(6) == {"list_channels", "join_channel"}
    # One notification when the tools appear, one when they go.
    assert [m["method"] for m in messages if m.get("method")] == [
        "notifications/tools/list_changed",
        "notifications/tools/list_changed",
    ]


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
