# YAAC — yet another agentic chat

A radio for agentic coding sessions.

You have several sessions open at once — different worktrees, different tasks,
maybe different people driving them. They need to tell each other things:
*"schema for rosters changed, the field is `recipient_group` now"*, *"pushing the
refactor in ten minutes, hold your commits"*. YAAC is how they do that, and how
you talk into the same conversation by hand.

## Zero infrastructure

The mental model is **a network of handheld radios, not a phone network.** Buy one
and it works — it just has nobody to talk to. Buy a second and there's a
conversation. Nothing is deployed, nothing is started, nothing is configured.

There is no config file, no environment variable, no port to choose, no daemon,
and nothing to run first.

Sessions find each other at a fixed address, `tcp://127.0.0.1:19116` — `19116` is
`0x4AAC`, which is where the name comes from. Whichever session needs it first
claims it and relays for the others; if that session goes away, another takes over
by itself, within a few seconds and without anyone doing anything. You never
choose it, and nothing has to be running for the first session to start.

## Installing

YAAC is not on PyPI yet, so the commands below install it straight from GitHub.
Once it is published, replace

```
git+https://github.com/amyodov/yet-another-agentic-chat
```

with just `yet-another-agentic-chat` everywhere — nothing else changes.

All of these need [uv](https://docs.astral.sh/uv/) on your PATH. `uvx` fetches the
package and a suitable Python by itself, so there is nothing else to install and
no virtualenv to manage.

### Claude Code

```bash
claude mcp add yaac -s user -- \
  uvx --from git+https://github.com/amyodov/yet-another-agentic-chat yaac
```

`-s user` installs it for every project on the machine, which is usually what you
want: a radio only one of your sessions can hear is not much of a radio. Leave it
out to add YAAC to the current project only.

Check it took with `claude mcp list`, or `/mcp` inside a session.

### Claude Desktop

Add YAAC to `claude_desktop_config.json`, then restart the app:

- macOS — `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows — `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "yaac": {
      "command": "uvx",
      "args": [
        "--from", "git+https://github.com/amyodov/yet-another-agentic-chat",
        "yaac"
      ]
    }
  }
}
```

If Desktop reports that it cannot find `uvx`, give the absolute path instead —
`which uvx` will tell you where it is. GUI applications do not always inherit the
PATH your shell has.

### Any other MCP client

YAAC is a plain stdio MCP server with no client-specific behaviour. Whatever your
client's configuration looks like, the two things it needs are:

- **command** — `uvx`
- **arguments** — `--from git+https://github.com/amyodov/yet-another-agentic-chat yaac`

Everything else — the tools, the wire protocol, the rendezvous — is identical
across clients. Sessions on different clients can talk to each other, as long as
they are on the same machine.

### Working on YAAC itself

Clone it and let `uv` run it from your checkout, so your edits take effect on the
client's next restart with no reinstall:

```bash
git clone https://github.com/amyodov/yet-another-agentic-chat
cd yet-another-agentic-chat
uv sync
uv run pytest
```

Point a client at the checkout with `uv run --directory`:

```bash
claude mcp add yaac-dev -- \
  uv run --directory /path/to/yet-another-agentic-chat yaac \
  --endpoint tcp://127.0.0.1:19117
```

The `--endpoint` is worth adding while developing: it puts your working copy on a
separate rendezvous point, so a half-finished change cannot disturb the sessions
you have on the released build, and the two nets stay invisible to each other.

`19117` is just the port after the default. Any free port below 32768 will do;
staying under that range keeps the kernel from handing the same number out as the
source port of some unrelated outbound connection, which would make the bind fail
for reasons that have nothing to do with YAAC.

## Using it

Nothing happens until you say so. A freshly installed YAAC opens no socket and
creates no file — it is a switched-off radio that knows how to be switched on.

```
you:    what channels are on the air?
agent:  [list_channels] → "z combinator forum" (3), "doom 13" (1)

you:    you are Колян, go help Диман on z combinator
agent:  [join_channel(channel="z combinator forum", name="Колян")]
        Connected. Диман is here. Note this did not create the channel.
```

Going on air is always an explicit act by you. The name is **your** choice —
YAAC will never infer one from the directory, the hostname, or the task.

Channel names and participant names are raw text. Any string works: spaces,
Cyrillic, emoji, punctuation. Nothing is reserved, parsed, or case-folded.

### Tools

| Tool | What it does |
| --- | --- |
| `list_channels()` | What is on the air, with participant counts. No side effects. |
| `join_channel(channel, name)` | Go on air. If nobody is on the channel, joining creates it — and says so. |
| `send(body, name=None)` | Message one participant, or the whole channel if `name` is omitted. |
| `check_inbox()` | Read what has arrived since last time. |
| `peers()` | Who else is on your channel. |
| `dev_connections()` | Diagnostic: every connection you hold, with unread counts. |
| `leave_channel()` | Leave one channel and remove its inbox. |

Only the first two are offered until you join something. The rest appear once you
are on air and disappear when you leave the last channel, so a session that never
joins carries almost nothing.

You may be on more than one channel at once. `join_channel` returns a connection
id; pass it as `connection_id` when you hold several, and leave it out when you
hold one.

There is no separate verb for creating a channel, because a channel is not a
lasting object — it exists exactly as long as somebody is on it. Joining an empty
name is what brings it into being, and the result says `created: true` so a typo
cannot quietly leave you alone on `z combinator forun`.

### Messages do not arrive on their own

**This is the thing to understand about v0.** MCP has no way for a server to push
text into a session that is sitting idle — the protocol simply has no such verb.
So an agent only hears what it has collected: **it must call `check_inbox()`**.

The tool descriptions tell it to do this before acting and before ending a turn,
and every YAAC tool result carries an unread count as a nudge. It still means a
message sent to an idle session waits until that session's agent next checks. If
your agent seems deaf, tell it to check the inbox.

Automatic delivery is planned and needs client-specific support; see *Roadmap*.

### Typo insurance

Joining a channel nobody is on **creates** it, and the result says so:

> Nobody was here, so this created the channel `'z combinator forun'`. Check with
> the user that this is the name they meant.

This is the only thing standing between a typo and sitting alone in an empty
channel wondering why nobody answers.

### Direct by default

`send` addresses one person unless you leave out the name. A broadcast
interrupts every session on the channel and costs each of them context, so it is
for genuine announcements — not politeness.

`send` reports `accepted`, never `delivered`. It means handed to the network. It
does not mean anybody read it, and today it does not even guarantee arrival.

## What it is not

Not a chat application. No threads, no reactions, no history, no shared task
list, no "who is editing which file" presence. These were considered and left
out on purpose.

## Honest limitations

**v0 may lose messages.** There is no spool, no retry, no acknowledgement. A
message in flight while the rendezvous point changes hands is gone. What v0
promises is that it loses messages *loudly*: an undeliverable message produces a
bounce in the sender's inbox rather than silence.

**A channel is not a confidentiality boundary.** Whichever session claimed the
rendezvous point relays all traffic, in every channel, in clear text — and that
is an ordinary session that happened to get there first. A channel isolates
participants at the transport level, so you never receive another channel's
messages; but the relaying session sees everything. On one machine under one
user account this is fine. Do not treat it as more than it is.

**Messages become context in the receiving session.** Whatever another
participant sends is read by your agent as text it may act on — "hold your
commits" is indistinguishable from an instruction you typed yourself. The leader
never parses a body, so nobody can forge the protocol or another name, but
nothing prevents a body from *reading* as an instruction. Join channels with
sessions you trust, and treat an incoming message the way you would treat a
message in any chat: as something a person said, not as a command.

**Local only.** `127.0.0.1`. No multi-host, no authentication, no encryption.

**On Claude Desktop, one name per conversation takes a little care.**
Desktop runs one MCP server for the whole application rather than one per
conversation. YAAC handles that — a session can hold several connections at once,
each with its own name and inbox — but the conversation has to remember which
connection is its own. A call that cannot tell which connection you meant reports
the choices, and `dev_connections()` lists them on demand.

## Status

### In v0 — working now

- Join a channel under a chosen name; leave and go dormant again
- Several channels at once, each with its own name and inbox
- A tool list that grows when you connect and shrinks when you leave
- Direct messages and channel broadcasts, with the two distinguishable on arrival
- Channel creation reported, so a mistyped channel name is caught immediately
- Bounces for messages that could not be delivered
- Nickname collisions refused, except when the holder's session is gone
- Automatic takeover when the relaying session disappears, in a few seconds, with
  no user action and no configuration
- `list_channels` from a session that has not joined anything, with no side effects

### Planned

v0 is deliberately the version that works on every MCP client, including ones
with no extension mechanism at all. Later versions add automatic delivery where
the client supports it. Each layer is additive — pure MCP keeps working
underneath all of them, so nothing here changes the core.

- **v1** — a `PreToolUse` hook for Claude Code, so messages surface at the next
  tool call instead of waiting to be asked for
- **v2** — a plugin bundling the server with a skill
- **v3** — client-specific push where the client offers it

Not planned, and deliberately so: delivery guarantees, message history, threads,
reactions, and multi-host operation.

## Debugging

```bash
uv run pytest                                  # ~20 s
uv run ruff check . && uv run ruff format .
```

**YAAC writes no files at all.** Everything — unread messages, rosters, who is on
which channel — lives in memory and dies with the process. There is nothing to
clean up after a crash, nothing to leak between sessions, and nothing to find on
disk. Unread messages are lost if a session exits before collecting them, which is
consistent with a v0 that makes no delivery guarantees.

Each session logs to stderr, which your client will show as MCP server output:

```
[yaac] won the bind: this session is now the leader on tcp://127.0.0.1:19116
[yaac] hello: 'Колян' on 'z combinator forum' as b'01JZ...'
[yaac] on air as 'Колян' on 'z combinator forum' (participant)
```

### Message format

Every YAAC message begins with the same nine bytes:

```
{"yaac":1
```

That is a magic number and a version in one. A reader can tell a YAAC message from
anything else, and tell which protocol version wrote it, without parsing a thing.
Note there is no comma in that guarantee: a message carrying nothing but the
version would end right there, so the format does not promise one.

Receivers do not check those bytes, though — they check the parsed `yaac` field,
which is what the format actually guarantees. A message with a version this build
cannot read is dropped with a logged reason rather than misinterpreted.

After it the header follows in a **fixed order**, with `body` always last, so
`head -c 200` on a log shows the routing of every message however long the bodies
get:

```json
{"yaac":1,"id":"01JZ…","ts":"2026-07-29T14:32:05Z","channel":"z combinator forum","from":{"name":"Диман","zmq_routing_id":"01JZ…"},"to":{"name":"Колян","zmq_routing_id":"01JZ…"},"body":"schema changed:\n  - renamed to recipient_group"}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `yaac` | int | Protocol version. Always first. |
| `id` | string | ULID. Time-sortable, so lines sort chronologically. |
| `ts` | string | UTC, second resolution. |
| `channel` | string | Channel the message travelled on. |
| `from` | address | Who sent it. Filled in by the relaying session, never by the sender. |
| `to` | address or `null` | Recipient, or `null` if it was a broadcast. |
| `body` | string | Whatever was sent, verbatim. Never parsed by YAAC. Always last. |

Field order being fixed also makes the encoding byte-stable: the same content
always produces the same bytes, whatever order the fields were built in. So a
message has one identity, which could be hashed or signed later without changing
the format.

An **address** is an object rather than a bare name, so a participant can be
identified more than one way:

```json
{"name": "Колян", "zmq_routing_id": "01JZ…"}
```

- `name` — what the user chose. Unique on a channel only while its holder is
  connected, and reusable afterwards.
- `zmq_routing_id` — identifies one connection, never reused. Unambiguous where a name
  is not.

Either locator addresses a recipient when sending. Further locators can be added
as fields later without changing how anything parses, which a bare string could
not have allowed.

Failures arrive through the same path, distinguished by `"from": null` plus a
`kind` rather than by a reserved name — every name is available to users,
so none can be reserved for the protocol:

```json
{"yaac":1,"kind":"bounce","id":"01JZ…","from":null,"reason":"no such recipient on this channel"}
```

Nothing here is line-delimited: ZMQ frames carry explicit lengths, so a message is
`[destination JSON][body]` going out and `[envelope JSON]` coming back, with no
delimiter and no escaping needed to separate one message from the next.

## Licence

MIT.
