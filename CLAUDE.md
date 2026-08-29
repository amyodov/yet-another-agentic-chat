# CLAUDE.md

See `README.md` for what YAAC does. This file is about working on it.

`README.md` also tracks features — what is in the current version and what is planned. Update its Status section
whenever you ship or defer something; it is the only record, as there is no separate roadmap or issue tracker.

## Vocabulary

| Term | Meaning |
| --- | --- |
| **frontend** | The MCP server: tool definitions, no state. |
| **backend** | In-process module: ZMQ sockets, inbox, roster cache. |
| **hat** | Whichever backend won the bind. Confers the job of passing messages along, and nothing else. |
| **participant** | Any backend on a channel, wearing the hat or not. |
| **membership** | One (channel, name) a process holds. A process may hold several. |
| **connection id** | A membership's routing id, returned by `join_channel` and used to address it. |
| **channel** | A named conversation. Addressing, not isolation. |
| **name** | A user-chosen name within a channel. Raw UTF-8. |
| **routing_id** | ULID set as the ZMQ `ROUTING_ID`; `zmq_routing_id` on the wire. |
| **address** | `{name, zmq_routing_id}`. How participants are named on the wire — never a bare string. |

ZMQ's own words stay ZMQ's own, prefixed: `zmq_routing_id`, not a YAAC synonym for it.

"frontend" and "backend" are relative to the MCP tool surface, not web layers. "channel" always means a YAAC channel,
never the Claude Code feature of the same name.

## Layout

```
src/yaac/
  frontend.py   MCP tool definitions, and the tool a Claude Code hook calls
  backend.py    sockets, bind election, receive loops, connection state
  hat.py        routing table, whois, roster, bounce
  protocol.py   envelope + control messages, serialization
  chat.py       the terminal client's entry point; imports textual only once it knows it is there
  chat_app.py   the terminal client itself
```

Python 3.14+, `uv`, `ruff`. Dependencies are `pyzmq` and `mcp` — ask before adding another.

To keep a file out of git, put `-nogit` in its name (`notes-nogit.md`, `probe-nogit.py`). `.gitignore` matches
`*-nogit*`, so don't add per-file entries to it.

Releases go through the `releasing` skill, and only on the user's explicit request — never on the assistant's
initiative. Changes to the tool definitions in `frontend.py` follow the `updating-mcp-tools` skill: descriptions
first, then it regenerates `docs/tools.md` — that file is generated and never edited by hand.

Line length 120, code and prose alike. Use current syntax freely: `match`/`case`, walrus, PEP 758 `except A, B:`.
Annotations are lazy in 3.14, so no `from __future__ import annotations`.

No nested `if`s where a flat shape exists: prefer `match`/`case` or an `if`/`elif`/`else` chain. Use the `else`
clause of `try`, `for`, and `while` when it says what the code means — `for`/`else` is exactly "the loop found
nothing".

## Verified facts

Measured here, not assumed. Re-measure before contradicting.

**`ROUTER_NOTIFY` is unusable and fails in a way that looks fine.** It is a libzmq draft option. The constant imports,
so a check for `hasattr(zmq, "ROUTER_NOTIFY")` passes, but `setsockopt` rejects it with `EINVAL` — released wheels
bundle libzmq built without drafts (`zmq.has('draft')` is `False`). True anywhere `pip install pyzmq` is used.

Replacements, both non-draft:
- **Arrival**: the participant's DEALER monitor fires `EVENT_CONNECTED`, and the participant re-sends `hello`. Covers
  the first connect too, so whoever picks up the hat next is told who is present without asking.
- **Departure**: `ROUTER_MANDATORY` raises `EHOSTUNREACH` when sending to a routing id that left. Eviction is lazy,
  detected on the first failed send — which is when we want to bounce anyway.

**Claude Code honours `tools/list_changed`, but only if the server advertised `tools.listChanged: true`.** The SDK's
`run_stdio_async()` hardcodes `NotificationOptions()` with every flag false, so `main` runs the low-level server
directly to override it. Advertising false and then sending the notification looks like a client bug and isn't —
check what `initialize` claimed first.

So the tool list is dynamic: dormant sessions list `list_channels` and `join_channel`, the other five appear on first
join and go on last leave.

**Not every client implements `tools/list_changed`, though it has been in MCP since 2024-11-05.** One that ignores
it would be stranded on a channel with tools it can never see, so `CLIENTS_THAT_NEVER_RELIST` in `frontend.py`
names the exceptions and hands them everything at connect; conforming clients keep the dynamic list. The key is
`clientInfo.name`, read off the wire with a probe server rather than guessed — check the same way before adding a
name. Which clients, and the upstream bugs, are in the comment there and in the README.

Detection wraps the `tools/list` handler: `initialize` is reserved by the SDK runner, which raises if
`add_request_handler` is given it, and the first listing is the earliest point where the client's name is known
and can still change the answer. `Server.middleware` would be tidier, and the SDK marks it provisional.

Cross-client delivery is measured, not argued: a Claude Code session and a Codex session exchanged messages both
ways on one channel, each running its own server process.

**MCP cannot push into an idle session — read off the specification rather than measured here, and true of
revision 2026-07-28, checked on 2026-08-28.** Every other entry in this section is a property of the world and
stays true; this one describes a document under active revision and will expire, so read the changelog rather
than this line. What it says today is that the direction of travel is away from push: a server may not initiate
a request at all, and the schema has no `ServerRequest` type to do it with; `sampling/createMessage`, roots and
logging are deprecated with a twelve-month window; and the one long-lived server→client stream,
`subscriptions/listen`, carries a closed set of four change notifications, three with empty parameters and one
carrying a URI and no body. Delivery is therefore pull-only, which is why the tool descriptions have to carry
the reminder to call `check_inbox`.

The roadmap's Triggers & Events working group is about telling a client that work it started has finished, not
about unsolicited content; the one proposal for re-entering a model's turn (SEP-2495) is open, unsponsored, and
answered by a maintainer with "the host owns the loop". That sentence is the whole reason delivery is a hook —
a hook is a host mechanism, and the host is what owns the loop.

**A Claude Code hook can call a tool on the MCP server it is bundled with, in the same process.** `type:
"mcp_tool"` names a `server` and a `tool`; for a plugin-bundled server the name is the scoped
`plugin:<plugin>:<server>`, not the bare key under `mcpServers`. The tool's *text* content is then read exactly as
a command hook's stdout: valid JSON is taken as a decision object, anything else as plain output. So `hook_report`
returns a JSON string rather than a result dict — a tool result shaped like a tool result parses fine, matches no
decision field, and is silently discarded.

Measured live on 2026-08-28, with the hooks wired into `.claude/settings.local.json` against a server running
from the working tree: a message sent by a second process arrived beside the next tool result on `PreToolUse`,
`check_inbox` then found the inbox empty, and `Stop` reopened a turn that had already ended. So the one
undocumented assumption holds -- **a tool absent from every `tools/list` can still be called by name from a
hook.** For a server that is not plugin-bundled the `server` field is the bare key, `yaac`, and Claude Code's
file watcher picks up a new hooks block with no restart.

**Codex reads the same contract, with one different door.** `codex-cli` 0.147.0 supports `command` and `mcp_tool`
handlers -- `prompt` and `agent` are parsed and skipped -- and joins an MCP tool's text blocks before running them
through the same output parser a command hook's stdout goes through, so returning a JSON string works there for
the same reason. Its binary carries `hookSpecificOutput`, `additionalContext` and `hookEventName`, so most events
are answered identically. `Stop` is the exception: its output schema admits no `hookSpecificOutput`, and text
reaches the model as `decision: "block"` with a `reason`, which Codex turns into a continuation prompt acting as a
new user prompt. `Stop` is spelled the same in both clients, so `hook_report` takes the contract as an argument
the hooks file sets rather than inferring it. Consumption still makes a loop impossible: the continuation re-fires
`Stop`, which finds nothing, so `stop_hook_active` needs no consulting.

**Claude Code captures an MCP server's stderr only while it connects.** It keeps a log per server under
`~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-<server>/`, and the startup line is there as
`"stderr: [yaac] dormant ..."` -- but `on air as ...` from a real join appears in no file this project has ever
written. So the log is not a place a running server can leave a trace for anything else to watch, and waking an
idle session needs a channel or a socket of our own rather than a tail. This is a client's internal cache layout,
undocumented and free to move.

That is why nothing about delivery touches the wire. The hook runs inside the process that owns the inbox, so
there is no second process to find, no socket to open, no session identifier to pass around, and the hat is not
involved at all. An earlier attempt had every backend push an unread tally to the hat so an out-of-process hook
could ask it — the hat kept a ledger of everyone's mailbox, which is not its job and cannot be kept true.

**Where a hook's `additionalContext` lands depends on the event**: next to the tool result for `PreToolUse` and
`PostToolUse`, alongside the prompt for `UserPromptSubmit`, and at the end of the turn for `Stop` — where, the
documentation says, "the conversation continues so Claude can act on the feedback". `Stop` is therefore the one
that can reopen a finished turn. Text placed there is in the model's context, which makes it a delivery and not a
notification: `hook_report` takes the messages, exactly as `check_inbox` does, and a second `Stop` then finds an
empty inbox, which is what makes a loop impossible without any `stop_hook_active` bookkeeping.

**A peer older than a control message answers nothing, and only ordering reveals it.** A 0.3.0 hat logs
`ignoring control message 'unread?'` and never replies, so a lone query waits out its whole timeout. Nothing
depends on this today, but it is the trap waiting for the envelope work in `docs/zmq.md`: pair a new query with
one every version has answered — `channels?` — and rely on a ROUTER handling one peer's messages in order, so a
`channels` reply arriving with no answer before it proves the peer read the new query and had nothing to say.
Measured against a real 0.3.0 hat, that turned a 1.0 s timeout into 0.13 s.

**A connect to a closed local port does not fail fast on Windows.** Measured in the field by Vadim: walking
eight candidate ports cost 8.2 s on every hook event, where the same walk on macOS and Linux is immediate --
`ECONNREFUSED` comes back on the loopback at once there. This is why nothing in YAAC probes a range of addresses:
the notice socket takes whatever port the kernel offers and publishes it, and the hat's `sessions` directory is
where a hook reads it.

Two other reasons the derivation it replaced was wrong, both properties of the world rather than of our code.
Codex keeps one `mcp_servers` block for a whole machine, so any per-session value put in its `env` is the same
value for every session -- a key derived from it made all of them answer for one inbox. And Node's `crypto`
declines to truncate blake2s, so a digest Python produced could not be reproduced there without hand-writing the
hash in another language.

**A session's app-server is its own forebear, so the wake address is discovered rather than configured.**
Measured on 2026-08-29: an MCP server started for a thread appears in the process tree as
`codex app-server --listen ws://… -> uv run … yaac -> python … yaac`, and a tool the model runs is a direct child
of the same app-server. So `wake.serving()` walks this process's ancestry and reads the address off the command
line of the nearest forebear carrying `--listen`. Verified end to end with a real app-server starting a real MCP
server: no variable in the environment, and `ws://127.0.0.1:4607` found.

This answers *which* app-server, not merely which ones are listening -- so two at once are no more ambiguous than
one, and the permanent `codex app-server daemon pid-update-loop`, which every Codex install runs and which
carries no `--listen`, is never a candidate. What must not be matched is the program name: that daemon would
answer to it always.

The reverse relation does not hold, and was checked first: an app-server with no thread running has **no**
descendants at all, and a hook is not obviously among them either -- project-local hooks are skipped entirely
until the project is trusted in `~/.codex/config.toml`, which is silent except for one line in the app-server's
stderr.

**Codex's app-server has two doors into a session, and the queue is the better one.** Measured against
codex-cli 0.151.0 on 2026-08-29, over `codex app-server --listen ws://127.0.0.1:4599`. `thread/queue/add` takes
`{threadId, input, clientUserMessageId}`; on an idle thread it drains at once and a whole turn runs, and on a
thread already working it waits its place. `turn/start` on a busy thread does not refuse -- it answers with a
second turn id, `status: inProgress`, alongside the first. So the queue is what YAAC knocks with, and
`turn/start` is only the fallback.

Two things it costs. `capabilities: {"experimentalApi": true}` in `initialize`, without which the queue answers
`-32600`; and `clientUserMessageId`, echoed back and read by nobody here, without which the call is refused for a
missing field. An app-server too old for the queue answers `-32600` naming the method --
``unknown variant `thread/queue/add` `` -- which is the only thing separating it from `-32600` for a thread that
does not exist. Both facts are visible in the binary's strings as well as on the wire.

Found by Vadim, who had the queue working before YAAC did; the busy-thread comparison and the fallback's
discrimination were measured here.

**Binding a busy port fails in ~0.4 ms** with `EADDRINUSE`. That is why every backend can just try.

**Windows needs the selector event loop** (found in pyzmq `zmq/asyncio.py`; measured since — the CI Windows job runs
the full suite). `zmq.asyncio` waits on sockets with `loop.add_reader`, which the default `ProactorEventLoop` lacks;
pyzmq then raises `RuntimeError` at first socket use unless tornado ≥ 6.1 is importable. `main` therefore passes
`loop_factory=asyncio.SelectorEventLoop` on win32 — `loop_factory`, not `set_event_loop_policy`, because 3.14
deprecates the policy API. The reverse constraint holds in tests: `asyncio.create_subprocess_exec` is proactor-only
on Windows, so `conftest.py` picks the loop per test module via the `pytest_asyncio_loop_factories` hook.

**The bind election stays single-winner on Windows** (found in libzmq `tcp_listener.cpp`; exercised by the CI
Windows job). Plain
`SO_REUSEADDR` there would let a second bind of an actively-bound port succeed; libzmq uses `SO_EXCLUSIVEADDRUSE`
on Windows instead, so exactly one ROUTER holds the endpoint on every OS.

**`CLAUDE_CODE_SESSION_ID` is visible to both the MCP server and hooks**, and equals the `session_id` a hook gets on
stdin. Better key than the cwd if v1 ever needs an out-of-process reader to find the right session, since it
separates two sessions in one directory. Absent on Claude Desktop. Nothing uses it today.

**Several memberships per process.** `Backend` holds a dict of `Membership`, each with its own routing id, DEALER,
roster and inbox, so the hat sees them as unrelated participants and the protocol needed no change. This matters
for clients running one server per application rather than per conversation, such as Claude Desktop — where it also
means one conversation can address another's connection, so `check_inbox` requires the id rather than guessing.

## Message format

The wire format itself is specified in `docs/message-format.md`; this section is the rules for code that touches it.
Its agreed successor — the envelope system, not yet built — is specified in `docs/zmq.md`.

`protocol.dumps` is the only serializer, and it stamps `yaac: PROTOCOL_VERSION` first on every top-level dict. That
ordering is the point: every message opens with `{"yaac":1`, a magic number a reader can key on without parsing. No
trailing comma is promised — a message with no other field ends there. `dumps` asserts its own output against
`MAGIC`, which is written out by hand so the two can be caught drifting apart.

Field order is otherwise whatever the `to_wire` constructors build, so keep `body` last in them: it is the only
unbounded field, and `head -c` on a log should show routing regardless of body size. Equal content gives equal bytes,
so a message has one identity to hash or sign later. Nothing computes a signature today.

`PROTOCOL_VERSION` marks the encoding generation, not the field list. Renaming or adding fields does not bump it;
replacing JSON with a binary framing would.

Received frames go through `protocol.parse`, never `loads` — it rejects a `yaac` field that is not exactly this
build's version, checking the parsed value rather than the leading bytes, since that is what the format guarantees.
`type(version) is not int` because `1.0` and `True` both equal `1`. Do not call `json.dumps` on anything that goes on the wire. `frontend.hook_report` is the one other caller, and it is answering Claude Code's hook contract rather than writing a YAAC message -- `protocol.dumps` would stamp `yaac: 1` into somebody else's format.

`from` and `to` are `Scope` objects whose fields compose — `{channel}`, `{peer}`, `{channel, peer}`, and `{}` for
the hat, which is the only mail it reads. A `peer` is an `Address`: `{name, zmq_routing_id}`, either locator
optional and the one you do not have simply left out. A name is unique on a channel only while its holder is
connected; a routing id identifies one connection and is never reused. Further locators can be added as fields
without breaking parsers, which is why these are structures.

One concept, one encoding: `null` and `{"channel": null}` are refused rather than read as the hat, and an address
naming nobody is refused rather than read as an address to nowhere. Unknown fields are ignored, because refusing
those would make every locator added later a breaking change.

`Address`, `Scope` and `Envelope` are frozen dataclasses with `to_wire`/`from_wire`. Parse with `from_wire`
rather than reading dict keys — it rejects a malformed address instead of coercing it.

## Hard rules

Each has a test.

1. **The MCP server writes nothing to stdout.** It carries the stdio transport; one `print()` breaks the session
   with a parse error. Diagnostics go to stderr through `logging`, which the entry points point there. The
   terminal client is the exception and owns its own
   stdout, which is why it is a separate entry point rather than a flag.
2. **Nothing is ever written to disk, and a dormant server opens no socket.** Unread messages, rosters and
   membership all live in memory and die with the process, so there is nothing to clean up after a crash.
   `Backend` is not constructed at all until `join_channel`.
3. **`send` never blocks.** A full queue or absent peer must raise. Blocking inside an MCP call freezes the session.
4. **Participant and channel names are never parsed, split, validated, or case-folded** by the hat or on the wire.
   That is why routing uses a separate opaque routing id: `ROUTING_ID` has length and byte constraints that
   user-chosen names must not inherit. The MCP boundary makes exactly one check, because it is the only layer that
   knows a human was meant to supply the value: it refuses a *completely* empty name, which is what an unexpanded
   template looks like rather than a choice. `"   "` is a name, and trimming it would be parsing.
5. **The hat never reads a body.** A body that looks like a control message gets delivered, not obeyed.
6. **`from` and the sender's channel come from the hat's table**, never from the sender. This is what makes
   cross-channel injection impossible rather than merely forbidden.

## Tests

**Parametrize hard.** Minimum number of test functions, maximum behaviour each. If a new test would differ from an
existing one only in its inputs, add a `parametrize` case instead.

**Compare values.** Asserting that something was called, or is truthy, or is non-empty, tests nothing — a stub
returning plausible shapes would pass.

**No second argument to `assert`.** It suppresses pytest's rewritten diff. Explain in a comment on the line above.

In `src/` the rule is the opposite: no rewriting, and `-O` strips asserts, so use them only for invariants that
cannot fail unless this code is wrong — `dumps` checking its own output against `MAGIC` is the case. Anything a peer
or a malformed frame can trigger must `raise`.

**ASCII names in tests** — `ann`, `bob`, `forum`. Non-ASCII appears only as parametrized data where encoding is what
is being tested. Docs and examples may use any UTF-8; the README's Cyrillic name shows that names are
unrestricted. Tool descriptions are mostly English but need not be only English.

## Prose

These rules cover everything written in words: comments, docstrings, commit messages, docs — and this file.

- Say what the mechanism, constraint, or failure mode is, with real names and numbers. Give the *why*; the *what*
  is in the code, and the next reader is as capable as you.
- Numbers that measure the world earn their place; numbers that count our own files do not. "Binding a busy port
  fails in ~0.4 ms" stays true forever, while "the version lives in four files" is wrong the moment a fifth
  appears — and it is a second copy of a fact, kept somewhere the thing it counts cannot reach. Enumerate, or
  state the invariant, and let a test name the exact set.
- Cover intent, not implementation. Text that restates the line below it says nothing and goes stale the first time
  that line changes. If it is clear from the code, do not repeat it.
- Technical vocabulary is free: "asynchronous queue with exclusive locking" is exactly right. Fancy general words
  are not — "think", not "ponder". No metaphors or slogans; the radio analogy lives in the README, not the source.
- Write "`ROUTER_MANDATORY` raises `EHOSTUNREACH` for an unknown routing id, so the send fails instead of being
  dropped", not "a stuck queue must fail, not grow silently".
- Commit messages: the subject names the change, the body gives the reason and what it displaces. Not a list of
  files touched — the diff already shows that.

## Design notes

- **The hat is put on by getting there first.** Soft state only, no policy, never configured, and it comes off when the
    process exits.
- **The hat connects its own DEALER to its own ROUTER.** Costs one socket, and keeps a single send path — no "am I
  the hat" branch anywhere.
- **Probing must not bind.** If `list_channels` bound, a session that only looked would become hat and drop the
  endpoint when the call returned. Its 10 s timeout is the only exit when nothing is listening, because ZMQ queues
  instead of failing. "Nobody on the air" is a normal answer.
- **Hold, don't bounce, during a changeover.** A message from an unknown routing id is parked and a `whois` sent, so the
  send succeeds late instead of failing. `pending` is bounded on count and age so a silent peer cannot leak memory.
- **No heartbeat, no TTL.** An idle net generates zero traffic.
- **TCP, not `ipc`.** An `ipc://` socket file survives `kill -9` and then blocks bind forever, needing a lock file
  and manual cleanup. The kernel frees a TCP port itself, and libzmq sets `SO_REUSEADDR`.
- **Port 19116** is `0x4AAC`, below the ephemeral range so the kernel won't hand it out as a source port.
- **`created` is derived from the roster**, not sent by the hat: channels are deleted when empty, so being alone on
  one means you just made it.

## Decided, not built

Settled in discussion with Alex; build only when told, ask before deviating.

- **Privacy is convention, not protection.** Everything runs on one machine under one user account, where any
  session can already read another's transcript from disk, so YAAC cannot add a boundary the OS does not have.
  Identity mechanisms exist to prevent accidents and default misuse by well-behaved participants — never to stop a
  determined session, and they must not claim otherwise.
- **Built on 2026-08-29:** the envelope system (one shape for every message,
  scopes that compose, `mentions` and `tags` as envelope fields, `payload` beside `body`), and peer identity
  (`peer_uid`, `peer_secret`, resume after a restart). `docs/message-format.md` is the reference; `docs/zmq.md`
  keeps the reasoning. Version 2 does not bridge to version 1, and both sides name the version they saw, because
  a mismatch otherwise reads as an empty net rather than as a disagreement.
- **Docs examples use the classical cast** — Alice, Bob, Carol; the hat-sees-everything caveat is "the hat is Eve
  by construction". One side note keeps a non-ASCII name to show names are unrestricted.

## Out of scope

Deferred on purpose — ask before adding.

- Outbox, SQLite, retries, acks, dedup, store-and-forward
- Delivery guarantees. v0 may lose messages, as long as it loses them loudly.
- Multi-host, CURVE auth, namespaced names
- Direct participant-to-participant connections after discovery
- Channel UUIDs as anything more than a reported field
- Presence beyond "who is on this channel now"
- Threads, reactions, history, shared task lists

## Reference

ZeroMQ's **Zyre** (RFC 36 / ZRE) solves nearly this problem: `ENTER`/`EXIT`, `JOIN`/`LEAVE`, `WHISPER`, `SHOUT`, over
DEALER-ROUTER. Worth reading before changing the protocol. Don't take the dependency — we need a subset, and `pyre`
is LGPLv3 and tracks Zyre 1.0.

Its better idea: separate discovery from data, so peers meet at a rendezvous point and then connect directly. That
removes the relay bottleneck and the confidentiality caveat in the README. Candidate for later.
