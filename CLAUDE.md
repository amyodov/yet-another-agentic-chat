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
  frontend.py   MCP tool definitions
  backend.py    sockets, bind election, receive loops, connection state
  hat.py        routing table, whois, roster, bounce
  protocol.py   envelope + control messages, serialization
```

Python 3.14+, `uv`, `ruff`. Dependencies are `pyzmq` and `mcp` — ask before adding another.

To keep a file out of git, put `-nogit` in its name (`notes-nogit.md`, `probe-nogit.py`). `.gitignore` matches
`*-nogit*`, so don't add per-file entries to it.

Releases go through the `releasing-to-pypi` skill, and only on the user's explicit request — never on the assistant's
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

**MCP cannot push into an idle session.** Nothing in the server→client set (`notifications/message`,
`resources/updated`, `list_changed`, `sampling/createMessage`, `elicitation/create`) reaches the model's context.
Delivery is pull-only, so the tool descriptions have to carry the reminder to call `check_inbox`.

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
`type(version) is not int` because `1.0` and `True` both equal `1`. Do not call `json.dumps` anywhere else.

`from` and `to` are `Address` objects, not strings: `{name, zmq_routing_id}`, either nullable. A name is unique on a
channel only while its holder is connected; a routing id identifies one connection and is never reused. Further
locators can be added as fields without breaking parsers, which is why this is a structure.

`Address`, `Destination` and `Envelope` are frozen dataclasses with `to_wire`/`from_wire`. Parse with `from_wire`
rather than reading dict keys — it rejects a malformed address instead of coercing it.

## Hard rules

Each has a test.

1. **Nothing is written to stdout.** It carries the stdio transport; one `print()` breaks the session with a parse
   error. Log to stderr via `backend.log`.
2. **Nothing is ever written to disk, and a dormant server opens no socket.** Unread messages, rosters and
   membership all live in memory and die with the process, so there is nothing to clean up after a crash.
   `Backend` is not constructed at all until `join_channel`.
3. **`send` never blocks.** A full queue or absent peer must raise. Blocking inside an MCP call freezes the session.
4. **Participant and channel names are never parsed, split, validated, or case-folded.** That is why routing uses a
   separate opaque routing id: `ROUTING_ID` has length and byte constraints that user-chosen names must not inherit.
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

Settled in discussion with Alex; build only when told, ask before deviating. The envelope system is formalized in
`docs/zmq.md` — that file is the spec, this section tracks the decisions and their open edges.

- **Privacy is convention, not protection.** Everything runs on one machine under one user account, where any
  session can already read another's transcript from disk, so YAAC cannot add a boundary the OS does not have.
  Identity mechanisms exist to prevent accidents and default misuse by well-behaved participants — never to stop a
  determined session, and they must not claim otherwise.
- **`join_channel` will return a `peer_uid` + `peer_secret` pair.** The secret is an honor-system convention, not
  cryptography: a participant that did not receive it through the proper flow is not that peer. The backend verifies
  it locally against its own memberships; the hat cannot verify anything, since its state is rebuilt from `hello`
  after every changeover. Presenting the pair on join resumes the same peer after a client restart. Still open:
  whether the secret gates `send` and `peers` or only inbox reads, and whether `peer_uid` becomes a locator inside
  `from`/`to`.
- **The world channel is `None`, not a name.** Omitted, null, or empty `channel` at the tool boundary all mean it,
  and the description says so. Distinguished structurally so no user-chosen string can clash with it and no
  English-centric default name exists. Costs to pay when building: the destination frame currently uses a null
  channel to mean "don't cross-check", `hello` requires a string channel, and `list_channels` needs a row for it.
- **The tool boundary validates; the protocol never does.** Hard rule 4 stays absolute for the hat and the wire,
  but the MCP layer refuses a completely empty `name` — empty is what an unexpanded template looks like, not a
  choice. Only completely empty: `"   "` is not empty, and trimming would be parsing. Rescope rule 4's wording
  when this is built.
- **A message becomes an object, not a string**: `payload` (any JSON — the tool description must say it may be
  anything; the readers are agents and will adapt), `tags` (topic), `mentions` (who is called on to react — while
  everyone in the delivery scope still hears it). Delivery scope (`to`) and social addressing (`mentions`) are
  separate things: a whisper stays private-scope, and mentioning someone on the open channel is heard by all, like
  radio. There is no urgency mechanism; being mentioned is the attention signal, and any loudness convention is a
  tag.
- **`from` and `to` are scope objects** whose fields compose: `{channel}` broadcasts to it, `{peer}` whispers,
  `{channel, peer}` is that peer as a member of that channel, and `to: {}` — no scope at all — addresses whoever
  wears the hat, for technical asks; symmetrically `from: {}` marks infrastructure messages such as bounces
  (today `from: null`). Senders never transmit `from` at all — the hat stamps it from its table (rule 6), so
  `from: {}` is unforgeable by construction, not by validation. Open: the world channel is a null channel on the
  wire, so `{}` and `{channel: null}` must not collapse — either strict absent-vs-null discipline in the
  serializer, or a dedicated field for the hat address. Also open: whether the message structure travels as an
  end-to-end body object the hat never decodes, or as envelope fields the hat copies.
- **One envelope for all wire traffic, control included.** `hello`, channel listing, `whois`, `roster`, bounces
  and chat all travel as the same mail shape; the control/data split by frame count disappears. Rule 5 restates
  as: the hat interprets exactly the mail addressed to `{}`, and nothing else — delivery versus obedience decided
  by addressing, not frame layout. What the receiving backend does with operator mail (roster to cache, bounce to
  inbox) is backend policy, not a second format. Decide at build time whether this framing unification bumps
  `PROTOCOL_VERSION`.
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
