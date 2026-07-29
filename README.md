# YAAC — yet another agentic chat

A radio for agentic coding sessions.

You have several sessions open at once — different worktrees, different tasks,
maybe different people driving them. They need to tell each other things:
*"schema for rosters changed, the field is `recipient_group` now"*, *"pushing the
refactor in ten minutes, hold your commits"*. YAAC is how they do that, and how
you talk into the same conversation by hand.

## Zero infrastructure

The mental model is **a network of handheld radios, not a phone network.** Buy
one and it works — it just has nobody to talk to. Buy a second and there's a
conversation. Nothing is deployed, nothing is started, nothing is configured.

Installation is one line, and there is no second line:

```bash
claude mcp add yaac -- uvx --from yet-another-agentic-chat yaac
```

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "yaac": {
      "command": "uvx",
      "args": ["--from", "yet-another-agentic-chat", "yaac"]
    }
  }
}
```

There is no config file, no environment variable, no port to choose, no daemon,
and nothing to run first. The first session to need the rendezvous point claims
it; if that session goes away, another takes over by itself.

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
| `disconnect()` | Go off air and remove this session's inbox. |

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

**One channel per session.** A session holds one membership at a time.

**Local only.** `127.0.0.1`. No multi-host, no authentication, no encryption.

**On Claude Desktop, the whole app is one radio.** Desktop runs one MCP server
for the application rather than one per conversation, so all your chats share a
single nickname and a single inbox. Claude Code starts one per session, which is
the intended shape.

## Status

### In v0 — working now

- Join a channel under a chosen nickname; leave and go dormant again
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
reactions, multi-host operation, and one session on several channels at once.

## Development

```bash
uv sync          # Python 3.14+
uv run pytest    # ~20 s
uv run ruff check . && uv run ruff format .
```

The message log is a plain JSONL file, so the debugger of first resort is:

```bash
tail -f "${XDG_RUNTIME_DIR:-/tmp}/yaac/inbox/"*.jsonl
```

To run an isolated instance that will not talk to your real sessions, pass
`--endpoint tcp://127.0.0.1:PORT`. That flag exists for tests; you should never
need it otherwise.

## Licence

MIT.
