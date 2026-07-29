# CLAUDE.md

See `README.md` for what YAAC does. This file is about working on it.

## Vocabulary

| Term | Meaning |
| --- | --- |
| **frontend** | The MCP server: tool definitions, no state. |
| **backend** | In-process module: ZMQ sockets, inbox, roster cache. |
| **hub** | The backend that won the bind. Not privileged. |
| **spoke** | Any backend that did not. |
| **channel** | A named conversation. Addressing, not isolation. |
| **nickname** | A user-chosen name within a channel. Raw UTF-8. |
| **handle** | ULID used as the ZMQ `ROUTING_ID`. Never shown to the user. |

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

**Claude Code ignores `tools/list_changed`.** A tool added at runtime never became callable, same turn or later turn.
So all six tools are always listed, and the on-air four return `not_connected` until connected. Don't try to hide them.

**MCP cannot push into an idle session.** Nothing in the server→client set (`notifications/message`,
`resources/updated`, `list_changed`, `sampling/createMessage`, `elicitation/create`) reaches the model's context.
Delivery is pull-only, so the tool descriptions have to carry the reminder to call `check_inbox`.

**Binding a busy port fails in ~0.4 ms** with `EADDRINUSE`. That is why every backend can just try.

**`CLAUDE_CODE_SESSION_ID` is visible to both the MCP server and hooks**, and equals the `session_id` a hook gets on
stdin. Better key than the cwd for a future out-of-process reader, since it separates two sessions in one directory.
Absent on Claude Desktop. `connect` records it in the inbox descriptor; v0 does not use it.

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
- One session on several channels. The design must not block it — hence handles as routing identity and inboxes keyed
  by handle — but v0 is one channel per session.
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
