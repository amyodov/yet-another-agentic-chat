# CLAUDE.md

See `README.md` for what YAAC does. This file is about working on it.

`README.md` also tracks features — what is in the current version and what is planned. Update its Status section
whenever you ship or defer something; it is the only record, as there is no separate roadmap or issue tracker.

## Vocabulary

| Term | Meaning |
| --- | --- |
| **frontend** | The MCP server: tool definitions, no state. |
| **backend** | In-process module: ZMQ sockets, inbox, roster cache. |
| **hub** | The backend that won the bind. Not privileged. |
| **spoke** | Any backend that did not. |
| **membership** | One (channel, nickname) a process holds. A process may hold several. |
| **connection id** | The membership's handle, returned by `connect` and used to address it. |
| **channel** | A named conversation. Addressing, not isolation. |
| **nickname** | A user-chosen name within a channel. Raw UTF-8. |
| **handle** | ULID used as the ZMQ `ROUTING_ID`. Also a locator inside an address. |
| **address** | `{nickname, handle}`. How participants are named on the wire — never a bare string. |

"frontend" and "backend" are relative to the MCP tool surface, not web layers. "channel" always means a YAAC channel,
never the Claude Code feature of the same name.

## Layout

```
src/yaac/
  frontend.py   MCP tool definitions
  backend.py    sockets, bind election, receive loops, connection state
  hub.py        routing table, whois, roster, bounce
  protocol.py   envelope + control messages, serialization
  inbox.py      jsonl + cursor + flock
```

Python 3.14+, `uv`, `ruff`. Dependencies are `pyzmq` and `mcp` — ask before adding another.

To keep a file out of git, put `-nogit` in its name (`notes-nogit.md`, `probe-nogit.py`). `.gitignore` matches
`*-nogit*`, so don't add per-file entries to it.

Line length 120, code and prose alike. Use current syntax freely: `match`/`case`, walrus, PEP 758 `except A, B:`.
Annotations are lazy in 3.14, so no `from __future__ import annotations`.

## Verified facts

Measured here, not assumed. Re-measure before contradicting.

**`ROUTER_NOTIFY` is unusable and fails in a way that looks fine.** It is a libzmq draft option. The constant imports,
so a check for `hasattr(zmq, "ROUTER_NOTIFY")` passes, but `setsockopt` rejects it with `EINVAL` — released wheels
bundle libzmq built without drafts (`zmq.has('draft')` is `False`). True anywhere `pip install pyzmq` is used.

Replacements, both non-draft:
- **Arrival**: the spoke's DEALER monitor fires `EVENT_CONNECTED`, and the spoke re-sends `hello`. Covers the first
  connect too, so a new hub is told who is present without asking.
- **Departure**: `ROUTER_MANDATORY` raises `EHOSTUNREACH` when sending to a handle that left. Eviction is lazy,
  detected on the first failed send — which is when we want to bounce anyway.

**Claude Code honours `tools/list_changed` — but only if the server advertises it.** An earlier version concluded
the opposite. It was wrong: the SDK's `run_stdio_async()` builds initialization options from `NotificationOptions()`
with every flag false, so we advertised `tools.listChanged: false` and then sent a change notification, which a
correct client ignores. `main` therefore runs the low-level server directly with
`NotificationOptions(tools_changed=True)`. Verified at the protocol level: after the notification the client sends a
fresh `tools/list` and can call the new tool.

So the tool list is dynamic. Dormant sessions list `list_channels` and `connect_to_channel`; the other five appear on
first connect and go on last disconnect. If you ever seem to find a client bug here, check what we advertised in
`initialize` before blaming the client.

**MCP cannot push into an idle session.** Nothing in the server→client set (`notifications/message`,
`resources/updated`, `list_changed`, `sampling/createMessage`, `elicitation/create`) reaches the model's context.
Delivery is pull-only, so the tool descriptions have to carry the reminder to call `check_inbox`.

**Binding a busy port fails in ~0.4 ms** with `EADDRINUSE`. That is why every backend can just try.

**`CLAUDE_CODE_SESSION_ID` is visible to both the MCP server and hooks**, and equals the `session_id` a hook gets on
stdin. Better key than the cwd for a future out-of-process reader, since it separates two sessions in one directory.
Absent on Claude Desktop. `connect` records it in the inbox descriptor; v0 does not use it.


**Several memberships per process.** `Backend` holds a dict of `Membership`, each with its own handle, DEALER, roster
and inbox, so the hub sees them as unrelated participants and no protocol change was needed. Tools take an optional
`connection_id`; with one membership open it may be omitted, with several it is required rather than guessed. This
matters for clients running one MCP server per application instead of per conversation, such as Claude Desktop.

## Message format

`protocol.dumps` is the only serializer. It sorts keys at every level and emits no insignificant whitespace, so equal
content gives equal bytes and a message can be hashed or signed later without a format change. Do not call
`json.dumps` anywhere else, and do not add a field whose value is not deterministically serializable.

`from` and `to` are `Address` objects, not strings: `{nickname, handle}`, either of which may be null. A nickname is
unique on a channel only while its holder is connected; a handle identifies one connection and is never reused. New
locators can be added as fields without breaking parsers, which is why this is a structure rather than a string.

`Address`, `Destination` and `Envelope` are frozen dataclasses with `to_wire`/`from_wire`. Parse with `from_wire`
rather than reading dict keys directly — it rejects a malformed address instead of silently coercing it.

## Hard rules

Each has a test.

1. **Nothing is written to stdout.** It carries the stdio transport; one `print()` breaks the session with a parse
   error. Log to stderr via `backend.log`.
2. **A dormant server opens no socket and creates no file.** `Backend` is not constructed until `connect_to_channel`.
3. **`send` never blocks.** A full queue or absent peer must raise. Blocking inside an MCP call freezes the session.
4. **Nicknames and channel names are never parsed, split, validated, or case-folded.** That is why routing uses a
   separate opaque handle: `ROUTING_ID` has length and byte constraints that user-chosen names must not inherit.
5. **The hub never reads a body.** A body that looks like a control message gets delivered, not obeyed.
6. **`from` and the sender's channel come from the hub's table**, never from the sender. This is what makes
   cross-channel injection impossible rather than merely forbidden.

## Tests

**Parametrize hard.** Minimum number of test functions, maximum behaviour each. If a new test would differ from an
existing one only in its inputs, add a `parametrize` case instead.

**Compare values.** Asserting that something was called, or is truthy, or is non-empty, tests nothing — a stub
returning plausible shapes would pass.

**No second argument to `assert`.** It suppresses pytest's diff, which explains the failure better. Put the
explanation in a comment on the line above.

**ASCII names in tests** — `ann`, `bob`, `forum`. Non-ASCII appears only as parametrized data where encoding is what
is being tested. Docs and examples may use any UTF-8; the README's Cyrillic nickname shows that names are
unrestricted. Tool descriptions are mostly English but need not be only English.

## Comments and docstrings

Say what the mechanism, constraint, or failure mode is, with real names and numbers. Give reasons, not just steps —
the next reader is as capable as you and needs the *why*, not an explanation of the obvious.

Use technical vocabulary freely: "asynchronous queue with exclusive locking" is exactly right. Do not reach for fancy
general words where plain ones work — "think", not "ponder". No metaphors or slogans; the radio analogy lives in the
README, not the source.

Write "`ROUTER_MANDATORY` raises `EHOSTUNREACH` for an unknown routing id, so the send fails instead of being dropped",
not "a stuck queue must fail, not grow silently".

## Design notes

- **The hub holds no authority.** Soft state only, no policy, never configured, any participant can become it.
- **The hub connects its own DEALER to its own ROUTER.** Costs one socket, and keeps a single send path — no "am I
  the hub" branch anywhere.
- **Probing must not bind.** If `list_channels` bound, a session that only looked would become hub and drop the
  endpoint when the call returned. Its 10 s timeout is the only exit when nothing is listening, because ZMQ queues
  instead of failing. "Nobody on the air" is a normal answer.
- **Hold, don't bounce, during a changeover.** A message from an unknown handle is parked and a `whois` sent, so the
  send succeeds late instead of failing. `pending` is bounded on count and age so a silent peer cannot leak memory.
- **No heartbeat, no TTL.** An idle net generates zero traffic.
- **TCP, not `ipc`.** An `ipc://` socket file survives `kill -9` and then blocks bind forever, needing a lock file
  and manual cleanup. The kernel frees a TCP port itself, and libzmq sets `SO_REUSEADDR`.
- **Port 19116** is `0x4AAC`, below the ephemeral range so the kernel won't hand it out as a source port.
- **`created` is derived from the roster**, not sent by the hub: channels are deleted when empty, so being alone on
  one means you just made it.

## Out of scope

Deferred on purpose — ask before adding.

- Outbox, SQLite, retries, acks, dedup, store-and-forward
- Delivery guarantees. v0 may lose messages, as long as it loses them loudly.
- Multi-host, CURVE auth, namespaced nicknames
- Direct spoke-to-spoke connections after discovery
- Channel UUIDs as anything more than a reported field
- Presence beyond "who is on this channel now"
- Threads, reactions, history, shared task lists

## Reference

ZeroMQ's **Zyre** (RFC 36 / ZRE) solves nearly this problem: `ENTER`/`EXIT`, `JOIN`/`LEAVE`, `WHISPER`, `SHOUT`, over
DEALER-ROUTER. Worth reading before changing the protocol. Don't take the dependency — we need a subset, and `pyre`
is LGPLv3 and tracks Zyre 1.0.

Its better idea: separate discovery from data, so peers meet at a rendezvous point and then connect directly. That
removes the relay bottleneck and the confidentiality caveat in the README. Candidate for later.
