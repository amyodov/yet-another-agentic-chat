"""The rules that are error conditions rather than behaviour.

These assert properties the design would be broken without, and each one exists
because getting it wrong is either invisible or catastrophic in production.
"""

import asyncio
import contextlib
import json
import sys
from pathlib import Path

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

    last_id = max((r["id"] for r in requests if "id" in r), default=0)
    lines: list[bytes] = []
    try:
        for request in requests:
            process.stdin.write((json.dumps(request) + "\n").encode())
        await process.stdin.drain()

        async def read_until_last_reply() -> None:
            while line := await process.stdout.readline():
                lines.append(line)
                if json.loads(line).get("id") == last_id:
                    return

        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(read_until_last_reply(), timeout=20)
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


async def test_startup_writes_only_json_rpc_to_stdout(endpoint):
    """stdout is the MCP stdio transport. One stray print kills the session with
    an opaque parse error, so every line there must be valid JSON-RPC."""
    stdout, stderr = await run_server(endpoint, HANDSHAKE)

    # decode() raises if anything non-JSON leaked onto the stream.
    assert {message.get("jsonrpc") for message in decode(stdout)} == {"2.0"}
    # Logging must still happen -- just on the other stream.
    assert b"[yaac]" in stderr


async def test_a_dormant_server_creates_no_files_and_opens_no_sockets(endpoint, isolated_runtime, tmp_path):
    """YAAC is installed in every session the user has. Almost all of them must
    stay completely inert: a switched-off radio leaves no trace."""
    runtime = tmp_path / "dormant-runtime"
    runtime.mkdir()
    requests = HANDSHAKE + [
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "peers", "arguments": {}},
        }
    ]
    stdout, _ = await run_server(
        endpoint,
        requests,
        env={
            "PATH": "/usr/bin:/bin",
            "XDG_RUNTIME_DIR": str(runtime),
            "PYTHONPATH": str(REPO / "src"),
        },
    )
    # The server really did run and answer, and still left nothing behind.
    assert [m.get("id") for m in decode(stdout)] == [1, 2, 3]
    assert list(runtime.iterdir()) == []


async def test_all_six_tools_exist_while_dormant(endpoint):
    """tools/list_changed is not honoured by Claude Code, so the on-air tools
    cannot appear later. They exist always and refuse until connected."""
    stdout, _ = await run_server(endpoint, HANDSHAKE)
    [tools] = [message["result"]["tools"] for message in decode(stdout) if message.get("id") == 2]
    assert {t["name"] for t in tools} == {
        "list_channels",
        "connect_to_channel",
        "send",
        "check_inbox",
        "peers",
        "disconnect",
    }
    # Tool descriptions are re-read by a model every session, so each must carry
    # one; an undescribed tool is one the model will not use correctly.
    assert [t["name"] for t in tools if not t["description"].strip()] == []


@pytest.mark.parametrize("tool", ["send", "check_inbox", "peers", "disconnect"])
async def test_on_air_tools_explain_themselves_while_dormant(endpoint, tool):
    """A refusal must tell the model what to do next, not just fail."""
    arguments = {"body": "x"} if tool == "send" else {}
    requests = HANDSHAKE + [
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    ]
    stdout, _ = await run_server(endpoint, requests)
    [result] = [m for m in decode(stdout) if m.get("id") == 3]
    # Refusing must be an ordinary answer the model can read and act on, not a
    # protocol-level error.
    assert "error" not in result
    assert "not_connected" in result["result"]["content"][0]["text"]
