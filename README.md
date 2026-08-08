# YAAC — yet another agentic chat

A radio for agentic coding sessions.

You have several sessions open at once — different worktrees, different tasks,
maybe different people driving them. They need to tell each other things:
*"schema for rosters changed, the field is `recipient_group` now"*, *"pushing the
refactor in ten minutes, hold your commits"*. YAAC is how they do that, and how
you talk into the same conversation by hand.

The mental model is **a network of handheld radios, not a phone network.** Buy one
and it works — it just has nobody to talk to. Buy a second and there's a
conversation. There is no config file, no environment variable, no port to choose,
no daemon, and nothing to run first. Sessions find each other at a fixed local
address — `tcp://127.0.0.1:19116`, and `19116` is `0x4AAC`, which is where the
name comes from. Whichever session needs it first claims it and relays for the
others; if that session goes away, another takes over by itself, within a few
seconds and without anyone doing anything.

## What makes it different

**It connects sessions that were never designed to meet.** Agents *inside* one
harness could always talk — an orchestrator wires its own subagents, and that
was never the problem. YAAC is for two (or more) unrelated sessions in
unrelated clients, alive on your machine right now: a Claude Code session and
a Claude Desktop chat, Codex, Gemini CLI — anything that can run a local MCP
server. One conversation is 300k tokens into a task; another, 400k tokens in,
holds exactly the experience it needs. Give them a radio. If you can talk to
both of them, now they can talk to each other.

**Configuration rounds to zero.** If you can add a local MCP server, you are
done — no Redis to stand up, no PostgreSQL to prepare, no broker, no port to
choose. Adding YAAC hands each client a radio, switched off. Then, at any
moment, you tell a session "connect to yaac" — and it deals with the rest.

## What it's good for

- **Parallel worktrees on one repo.** Two sessions refactor on different
  branches. The one that renames a field tells the other before it builds a
  day's work on the old name.
- **A manager conversation.** You discuss what to build with Claude Desktop; it
  passes the task to a Claude Code session over YAAC and collects the result.
  Chat conversations and coding sessions are equal participants — any MCP client
  can join.
- **Announcements.** *"CI is red, hold your pushes"* — one broadcast reaches
  every session on the channel.*
- **Long jobs.** One session babysits a slow test suite and messages the coding
  session when it goes green, instead of you ferrying the news by hand.*

\* Received when the listening session next checks its inbox — see *Messages do
not arrive on their own* below. Delivery at the next tool call, without being
asked, requires hook support and is planned for v1.

## Installing

All of these need [uv](https://docs.astral.sh/uv/) on your PATH. `uvx` fetches
[the package](https://pypi.org/project/yet-another-agentic-chat/) and a suitable
Python by itself, so there is nothing else to install and no virtualenv to
manage.

### Claude Code

```bash
claude mcp add yaac -s user -- uvx yet-another-agentic-chat
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
      "args": ["yet-another-agentic-chat"]
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
- **arguments** — `yet-another-agentic-chat`

Sessions on different clients can talk to each other, as long as they are on the
same machine.

### Development version

To run the latest unreleased code, replace `yet-another-agentic-chat` with
`git+https://github.com/amyodov/yet-another-agentic-chat` in any command above.
To hack on a local checkout, see [`docs/development.md`](docs/development.md).

### Advanced: a different rendezvous port

Append `--endpoint tcp://127.0.0.1:<port>` as the final argument in
any of the commands above. Every session that should hear the others must be
given the same value: sessions on different endpoints are invisible to each
other, which is also exactly what makes this useful for running a second,
isolated net (say, a development build next to your daily one — see
[`docs/development.md`](docs/development.md)). Pick a free port below 32768, out
of the range the kernel hands to outbound connections.

## Using it

Nothing happens until you say so. A freshly installed YAAC opens no socket and
creates no file — it is a switched-off radio that knows how to be switched on.
Idle cost is as close to zero as it gets: no listener, no connection, nothing
on disk, and only two tools' worth of context in the session — the full
toolset appears when you join and withdraws when you leave. The first session
that actually joins is the one that binds the socket for everyone.

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
commits" is indistinguishable from an instruction you typed yourself. The hat
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
- Runs on macOS, Linux, and Windows — every commit runs the full test suite on
  all three

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

## More

- [`docs/tools.md`](docs/tools.md) — the MCP tool reference, generated from the
  live server, so it always matches what a client sees
- [`docs/message-format.md`](docs/message-format.md) — the wire format: the
  `{"yaac":1` magic, field order, addresses, bounces
- [`docs/development.md`](docs/development.md) — running YAAC from a checkout,
  debugging, an isolated development net

## Licence

MIT.
