"""The rules that are error conditions rather than behaviour.

These assert properties the design would be broken without, and each one exists
because getting it wrong is either invisible or catastrophic in production.
"""

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from yaac import frontend
from yaac.backend import (
    LOG_QUEUE,
    ConnectionRefused,
    add_rendezvous_flags,
    chosen_rendezvous,
    configure_logging,
)

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
        for step in requests:
            # A callable is given every answer so far: a test that must present a value the server invented --
            # a connection id, a peer secret -- cannot write it into a list prepared in advance.
            if callable(step):
                answered = [json.loads(line) for line in lines]
                request = step({m["id"]: m for m in answered if "id" in m})
            else:
                request = step
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


async def test_the_wire_says_the_secret_has_to_outlive_a_compaction(endpoint: str) -> None:
    """The one fact a model cannot recover by reasoning, in the only place that always reaches it.

    A tool description rides in every request, so it survives a compaction where a tool result does not -- and a
    result is where this used to be said. What compaction takes is the value, and a model with no warning has no
    reason to treat an opaque string as worth carrying. Asserted off `tools/list` rather than off the constants,
    because the description a client actually receives is the one that matters.
    """
    stdout, _ = await run_server(endpoint, handshake("codex-mcp-client"))
    [tools] = [m["result"]["tools"] for m in decode(stdout) if m.get("id") == 2]
    described = {t["name"]: t for t in tools}

    # Where the secret is handed out, said in the description itself.
    joining = described["join_channel"]["description"]
    assert "MUST survive any\ncompaction" in joining and "call\nthis again with the same channel and name" in joining

    # And on every tool that asks for it back, since that is the argument a compacted model has to produce.
    for name in ("send", "peers", "check_inbox", "leave_channel"):
        asked = described[name]["inputSchema"]["properties"]["peer_secret"]["description"]
        assert "verbatim through any compaction" in asked


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

    def leave(answers: dict) -> dict:
        """Leaving needs the secret the join returned, which is what keeps a session from stranding itself."""
        joined = json.loads(answers[3]["result"]["content"][0]["text"])
        return {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "leave_channel", "arguments": {"peer_secret": joined["peer_secret"]}},
        }

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


@pytest.mark.parametrize("joins", [True, False], ids=["wearing-the-hat", "never-joined"])
async def test_a_server_whose_client_left_lets_go_of_the_port(endpoint: str, joins: bool) -> None:
    """A client going away is the ordinary end of every session, and the process has to actually end.

    It did not. `Backend.close` terminated the ZMQ context without closing the sockets in it, and termination
    waits for exactly that, so the process survived its client -- measured once at three days and eighteen hours,
    still holding the rendezvous port with its event loop long gone. Every session on the machine then found a
    port that accepts connections and answers nothing, which reads as an empty network rather than a broken one.

    The port is the assertion, not the exit code: a hat that cannot be replaced is the part that hurts other
    sessions. Unconditional, whatever the process was doing -- losing stdin means the client that owns it is gone
    and no request can ever arrive again, so there is nothing left for it to be. A hat is no reason to stay: the
    election hands the endpoint to whoever binds next, in about two seconds. A membership is a reason to leave,
    since a name held by a process nobody can reach swallows every message sent to it.
    """
    port = int(endpoint.rsplit(":", 1)[1])
    process = await asyncio.create_subprocess_exec(
        *SERVER,
        "--endpoint",
        endpoint,
        cwd=REPO,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    assert process.stdin and process.stdout

    join = [
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "tools/call",
            "params": {"name": "join_channel", "arguments": {"channel": "forum", "name": "ann"}},
        }
    ]
    for request in [*handshake(), *(join if joins else [])]:
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


@pytest.mark.parametrize(
    "call,expected",
    [
        (lambda: frontend.join_channel(None, "forum", ""), "name is empty"),
        (lambda: frontend.send("hello", name=""), "recipient name is empty"),
    ],
    ids=["join", "send"],
)
async def test_the_tool_boundary_refuses_a_name_nothing_filled_in(call, expected: str) -> None:
    """The one check a name ever gets, and it lives here rather than on the wire: rule 4 keeps the hat and the
    protocol out of names entirely, and only this layer knows a human was meant to supply the value. A completely
    empty name is what an unexpanded template looks like -- and on `send` it would be worse than useless, since
    omitting the name is how you address everyone, so an unfilled one would broadcast what was meant for one peer.

    Refused as a result the model can act on, not an exception: the caller's next move is to ask its user.
    """
    answer = await call()
    assert answer["error"] == expected
    assert "next_step" in answer  # a refusal that does not say what to do next is a dead end


@pytest.mark.parametrize("name", ["   ", "\t", "a", "  Bob  "])
async def test_a_name_that_is_not_empty_reaches_the_backend_untouched(monkeypatch, name: str) -> None:
    """Only *completely* empty is refused. Whitespace is a name a user may have chosen, and trimming it to find out
    would be parsing, which rule 4 forbids -- so what the tool passes down must be what it was given, byte for byte.

    The backend is a stub that records and refuses: this asserts what crossed the boundary, and touches no network.
    """
    passed: list[str] = []

    class Recording:
        memberships = {"already": object()}  # so the tool-list notification path is not taken

        async def connect(self, channel: str, name: str, peer_uid=None, peer_secret=None):
            passed.append(name)
            raise ConnectionRefused("recorded")

    monkeypatch.setattr(frontend, "radio", Recording)
    answer = await frontend.join_channel(None, "forum", name)
    assert passed == [name]
    assert answer["error"] == "recorded"


@pytest.mark.parametrize(
    "mentions,accepted",
    [(["Bob"], True), (["Bob", "Carol"], True), ([], True), ([""], False), ([42], False), (None, True)],
    ids=["one", "several", "empty-list", "empty-name", "not-a-name", "omitted"],
)
async def test_mentions_are_checked_before_anything_is_queued(monkeypatch, mentions, accepted: bool) -> None:
    """A malformed mention is refused where the model can read the reason, not sent for the hat to puzzle over.
    An empty name is the unexpanded template again; an empty list is simply nobody, which is what omitting it
    means, so both are accepted and neither is written to the wire."""
    sent: list[tuple] = []

    class Recording:
        memberships = {"already": object()}

        @staticmethod
        def verify(_membership, _secret):
            """The stub holds no secret; what is under test here is what crosses the boundary, not the gate."""

        @staticmethod
        def resolve(_):
            class Membership:
                name = "Alice"
                routing_id = "01A"

                @staticmethod
                def peer_names():
                    return ["Bob"]

                @staticmethod
                async def send(body, name=None, **kwargs):
                    sent.append((body, name, kwargs))
                    return "01M"

                @staticmethod
                def pending_count():
                    return 0

            return Membership()

        @staticmethod
        def describe_all():
            return []

    monkeypatch.setattr(frontend, "radio", Recording)
    answer = await frontend.send("hi", mentions=mentions)
    assert (answer["status"] == "accepted") is accepted
    if accepted:
        assert [m.name for m in sent[0][2]["mentions"]] == list(mentions or [])
    else:
        assert "next_step" in answer  # a refusal that does not say what to do next is a dead end


async def test_a_mention_of_somebody_absent_is_carried_and_reported(monkeypatch) -> None:
    """Not refused: a mention is social rather than delivery, and "Bob, if you are here" is a normal thing to
    say. But nothing is stored for a session that is not connected, so the sender is told who was not listening."""

    class Recording:
        memberships = {"already": object()}

        @staticmethod
        def verify(_membership, _secret):
            """The stub holds no secret; what is under test here is what crosses the boundary, not the gate."""

        @staticmethod
        def resolve(_):
            class Membership:
                name = "Alice"
                routing_id = "01A"

                @staticmethod
                def peer_names():
                    return ["Bob"]

                @staticmethod
                async def send(body, name=None, **kwargs):
                    return "01M"

                @staticmethod
                def pending_count():
                    return 0

            return Membership()

        @staticmethod
        def describe_all():
            return []

    monkeypatch.setattr(frontend, "radio", Recording)
    answer = await frontend.send("hi", mentions=["Bob", "Carol"])
    assert answer["status"] == "accepted"
    assert answer["mentioned_but_absent"] == ["Carol"]


class Blocked(io.RawIOBase):
    """A pipe nobody is reading: every write waits, exactly as a full 64 KB kernel buffer makes it wait."""

    def __init__(self) -> None:
        self.wedged = threading.Event()  # never set; a write that reaches here does not come back

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.wedged.wait()
        return len(data)


def test_logging_cannot_block_the_session(monkeypatch) -> None:
    """Hard rule 3 -- nothing may block the session -- applied to diagnostics, which is where it was missing.

    A client that pipes stderr and stops reading it leaves a 64 KB buffer and no more; Claude Code captures a
    server's stderr only while it connects. The write that fills it blocks inside the event loop, and from
    outside the session simply stops answering: the transport dies and every tool reports it, including the
    diagnostics somebody would reach for. Measured against a real server before the fix: 3000 warnings, and it
    never answered again.

    A mixed-version net produces that flood on its own, since a hat logs a warning for every operator message it
    does not know and a Codex hook asks on every tool call. So the queue drops lines rather than waiting: a
    radio that stops relaying because nobody read its log has failed at the only job it has.
    """
    blocked = Blocked()
    monkeypatch.setattr(sys, "stderr", io.TextIOWrapper(blocked))
    logger = logging.getLogger("yaac")
    handlers = list(logger.handlers)
    try:
        logger.handlers.clear()
        configure_logging()

        # In a thread, so that a regression is a failed assertion rather than a suite that hangs -- which is how
        # this class of bug cost fifty minutes of a Windows CI job once already.
        done = threading.Event()

        def shout() -> None:
            for number in range(LOG_QUEUE * 3):
                logger.warning("ignoring operator message %r", number)
            done.set()

        threading.Thread(target=shout, daemon=True).start()
        assert done.wait(timeout=20)
    finally:
        # Let the writer thread out before leaving. `logging.shutdown` flushes every handler at interpreter
        # exit, so a stream still wedged here would stop pytest itself from ending -- which is the same fact
        # this test is about, met from the other side.
        blocked.wedged.set()
        logger.handlers[:] = handlers


@pytest.mark.parametrize(
    "given,endpoint,role",
    [
        ([], "tcp://127.0.0.1:19116", "rendezvous"),
        (["--rendezvous", "tcp://127.0.0.1:1"], "tcp://127.0.0.1:1", "rendezvous"),
        (["--bind", "tcp://127.0.0.1:2"], "tcp://127.0.0.1:2", "bind"),
        (["--connect", "tcp://127.0.0.1:3"], "tcp://127.0.0.1:3", "connect"),
        (["--endpoint", "tcp://127.0.0.1:4"], "tcp://127.0.0.1:4", "rendezvous"),
    ],
    ids=["default", "rendezvous", "bind", "connect", "the-old-name"],
)
@pytest.mark.parametrize("program", ["yaac", "yaac-chat"])
def test_both_entry_points_read_the_same_three_flags(program: str, given: list[str], endpoint: str, role: str) -> None:
    """One address, three spellings, and the same three wherever you type them.

    Two programs join one net, so a flag that worked on one and not the other would be a net that behaves
    differently depending on which reached it first. `--endpoint` is the name this had before it was one of
    three; it is kept working and kept out of the help, so only one spelling is taught.
    """
    parser = argparse.ArgumentParser(prog=program)
    add_rendezvous_flags(parser)
    assert chosen_rendezvous(parser.parse_args(given)) == (endpoint, role)


@pytest.mark.parametrize(
    "given", [["--bind", "tcp://127.0.0.1:1", "--connect", "tcp://127.0.0.1:2"], ["--bind", "x", "--rendezvous", "y"]]
)
def test_two_of_the_three_at_once_is_refused(given: list[str]) -> None:
    """They name one address and contradict each other about it, so there is no answer to pick."""
    parser = argparse.ArgumentParser(prog="yaac")
    add_rendezvous_flags(parser)
    with pytest.raises(SystemExit):
        parser.parse_args(given)


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


@pytest.mark.parametrize("name", ["форум", "会議", "forum"], ids=["cyrillic", "han", "ascii"])
def test_a_log_line_carries_a_name_a_console_cannot_spell(monkeypatch, name: str) -> None:
    """Every log line quotes a channel or participant name, which is raw UTF-8, while the console's encoding is
    whatever the OS chose -- cp1251 on a Russian Windows. A handler writing through `sys.stderr` there raises
    inside `emit`, and `logging` answers with "--- Logging error ---" instead of the line, losing exactly the
    diagnostic somebody was reading the log for."""
    console = io.TextIOWrapper(io.BytesIO(), encoding="cp1251", errors="strict")
    monkeypatch.setattr(sys, "stderr", console)
    logger = logging.getLogger("yaac")
    handlers = list(logger.handlers)
    try:
        logger.handlers.clear()
        configure_logging()
        logger.info("on air as %r", name)
        # A line is written by the queue's own thread, so waiting for it is part of what logging now is.
        wanted = f"[yaac] on air as {name!r}"
        deadline = time.monotonic() + 10
        while wanted not in console.buffer.getvalue().decode("utf-8") and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        logger.handlers[:] = handlers

    assert wanted in console.buffer.getvalue().decode("utf-8")
