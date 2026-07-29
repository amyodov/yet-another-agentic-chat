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
and nothing to run first. The first session that needs the rendezvous point claims
it; if that session goes away, another takes over by itself.

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
  --endpoint tcp://127.0.0.1:19216
```

The `--endpoint` is worth adding while developing: it puts your working copy on a
separate rendezvous point, so a half-finished change cannot disturb the sessions
you have on the released build, and the two nets stay invisible to each other. Any
free port below the ephemeral range will do.

## Using it

Nothing happens until you say so. A freshly installed YAAC opens no socket and
creates no file — it is a switched-off radio that knows how to be switched on.

```
you:    what channels are on the air?
agent:  [list_channels] → "z combinator forum" (3), "doom 13" (1)

you:    you are Колян, go help Диман on z combinator
agent:  [connect_to_channel(channel="z combinator forum", nickname="Колян")]
        Connected. Диман is here. Note this did not create the channel.
```

Going on air is always an explicit act by you. The nickname is **your** choice —
YAAC will never infer one from the directory, the hostname, or the task.

Channel names and nicknames are raw text. Any string works: spaces, Cyrillic,
emoji, punctuation. Nothing is reserved, parsed, or case-folded.

### Tools

| Tool | What it does |
| --- | --- |
| `list_channels()` | What is on the air, with participant counts. No side effects. |
| `connect_to_channel(channel, nickname)` | Go on air. Creates the channel if empty, and says so. |
| `send(body, nickname=None)` | Message one participant, or the whole channel if `nickname` is omitted. |
| `check_inbox()` | Read what has arrived since last time. |
| `peers()` | Who else is on your channel. |
| `connections()` | Your open connections, with unread counts. |
| `disconnect()` | Leave a channel and remove its inbox. |

Only the first two are offered until you connect. The rest appear once you are on
air and disappear when you leave, so a session that never joins a channel carries
almost nothing.

You may join more than one channel at once. `connect_to_channel` returns a
connection id; pass it as `connection_id` when you hold several, and leave it out
when you hold one.

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

`send` addresses one person unless you leave out the nickname. A broadcast
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
commits" is indistinguishable from an instruction you typed yourself. The hub
never parses a body, so nobody can forge the protocol or another nickname, but
nothing prevents a body from *reading* as an instruction. Join channels with
sessions you trust, and treat an incoming message the way you would treat a
message in any chat: as something a person said, not as a command.

**Local only.** `127.0.0.1`. No multi-host, no authentication, no encryption.

**On Claude Desktop, one nickname per conversation takes a little care.**
Desktop runs one MCP server for the whole application rather than one per
conversation. YAAC handles that — a session can hold several connections at once,
each with its own nickname and inbox — but the conversation has to remember which
connection is its own. `connections()` lists them if it loses track.

## Status

### In v0 — working now

- Join a channel under a chosen nickname; leave and go dormant again
- Several channels at once, each with its own nickname and inbox
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

The message log is a plain JSONL file, one envelope per line, so the debugger of
first resort is:

```bash
tail -f "${XDG_RUNTIME_DIR:-/tmp}/yaac/inbox/"*.jsonl
```

A file appears there only while a connection is open, and is deleted on
`disconnect`, so an empty directory means nothing is on air.

### Message format

Each line of an inbox file is one complete JSON object, newline-terminated —
**always exactly one line per message**, whatever the body contains, because JSON
escapes control characters. `jq` and `wc -l` both do the obvious thing.

The encoding is **canonical**: keys sorted at every level, no insignificant
whitespace, UTF-8 rather than `\u` escapes. Equal content therefore always
produces equal bytes, so a message has a stable identity that can be hashed or
signed without renegotiating the format.

```json
{"body":"schema changed:\n  - renamed to recipient_group","channel":"z combinator forum","from":{"handle":"01JZ...","nickname":"Диман"},"id":"01JZ...","to":{"handle":"01JZ...","nickname":"Колян"},"ts":"2026-07-29T14:32:05Z"}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `body` | string | Whatever was sent, verbatim. Never parsed by YAAC. |
| `channel` | string | Channel the message travelled on. |
| `from` | address | Who sent it. Filled in by the relaying session, never by the sender. |
| `id` | string | ULID. Time-sortable, so lines sort chronologically. |
| `to` | address or `null` | Recipient, or `null` if it was a broadcast. |
| `ts` | string | UTC, second resolution. |

An **address** is an object rather than a bare name, so a participant can be
identified more than one way:

```json
{"handle": "01JZ...", "nickname": "Колян"}
```

- `nickname` — what the user chose. Unique on a channel only while its holder is
  connected, and reusable afterwards.
- `handle` — identifies one connection, never reused. Unambiguous where a
  nickname is not.

Either locator addresses a recipient when sending. Further locators can be added
as fields later without changing how anything parses, which a bare string could
not have allowed.

Failures arrive in the same file, distinguished by `"from": null` plus a `kind`
rather than by a reserved nickname — every nickname is available to users, so none
can be reserved for the protocol:

```json
{"from":null,"id":"01JZ...","kind":"bounce","reason":"no such recipient on this channel"}
```

Writers append whole lines, but a reader can still arrive mid-flush. Consume only
up to the last newline and leave the remainder for next time; that is what
`check_inbox` does.

On the wire it is not line-based at all — ZMQ frames carry explicit lengths, so a
message is `[destination JSON][body]` going out and `[envelope JSON]` coming back,
with no delimiter and no escaping.

## Licence

MIT.
