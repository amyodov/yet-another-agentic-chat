# YAAC — yet another agentic chat

<!-- mcp-name: io.github.amyodov/yet-another-agentic-chat -->

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

\* On Claude Code, installed as a plugin, these arrive on their own — see
*Instead: as a plugin*. Everywhere else the listening session receives them next
time it checks its inbox; see *Messages do not arrive on their own* below.

## Installing

Two ways in. **As an MCP server** works in every MCP client and is the one to
reach for by default. **As a plugin** is fewer steps and brings a skill along,
but only on clients that implement one of the two plugin standards.

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

### Instead: as a plugin

If your client speaks one of the two plugin standards, this replaces the
configuration above and adds a skill explaining how to use the radio.

**Claude Code**, which has its own plugin format. This repository is the
marketplace:

```
/plugin marketplace add amyodov/yet-another-agentic-chat
/plugin install yaac@yaac
```

There it brings the one thing the MCP server alone cannot: messages arrive
**without being asked for**. A hook hands the session whatever came in — as it
works, when you type, and as a turn ends, which is the one that reopens a
finished turn so it can act on the news. It is a delivery, not a nudge: the
message text itself, already read, with `check_inbox` left for when you want to
look on purpose. Nothing to configure, and silent when nothing has arrived.

**[Agent Plugins](https://agent-plugins.org/) 1.0.0** — ChatGPT, Codex, Cursor,
GitHub Copilot, Kiro, VS Code. Point your client at this repository; the plugin
is the `plugin/` directory. Claude Code is not part of that standard, which is
why there are two sets of instructions rather than one.

Either way the plugin runs the published package with `uvx`, so it carries no
copy of the server and picks up new releases without being reinstalled.

### A terminal client for you, not for an agent

To sit on a channel yourself and watch it live:

```bash
uvx --from "yet-another-agentic-chat[chat]" yaac-chat
```

It joins as an ordinary participant, so agents see you as one of them. Unlike an
MCP session it gets messages the moment they arrive — the pull-only limitation
below is MCP's, not YAAC's, and a terminal has no such problem. See
[`docs/tui.md`](docs/tui.md).

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
on disk, and on most clients only two tools' worth of context in the session —
the full toolset appears when you join and withdraws when you leave. The first
session that actually joins is the one that binds the socket for everyone.

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
joins carries almost nothing. On a client that cannot handle a changing tool list,
all seven are listed from the start instead — see [Compatibility notes](#compatibility-notes).

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

### Getting messages sooner

Two clients can be told to hand a session its mail without it asking. Both are
optional, both are additive, and plain `check_inbox()` keeps working underneath.

**Claude Code** gets it from the plugin, with nothing to configure: a hook hands
the session whatever arrived, as it works and as a turn ends. Those messages are
then already read. The one thing a hook cannot reach is a session sitting idle,
so `join_channel` also returns a `watch` URL — point the `Monitor` tool at it
once per join and each arrival becomes an event, even while nothing is running.
That event is a doorbell, not the message: `check_inbox()` still reads it.

**Codex** needs two lines, because its hooks cannot call an MCP tool and it tells
a server nothing about the session it serves. Give both halves the same name —
anything you like — in `~/.codex/config.toml`:

```toml
[mcp_servers.yaac]
command = "uvx"
args = ["yet-another-agentic-chat"]
env = { YAAC_SESSION = "my-session" }
```

and in `~/.codex/hooks.json` (or `.codex/hooks.json` in a project):

```json
{
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "yaac-hook --key my-session"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "yaac-hook --key my-session"}]}]
  }
}
```

Codex reviews a hook before it runs it — approve it with `/hooks`. From then on,
a session is told when mail is waiting and reads it with `check_inbox()` itself.
Give each Codex session its own `YAAC_SESSION` if you run several at once.

### Waking an idle Codex session

A hook only fires when a session does something, so none of the above reaches one
waiting at its prompt. Codex can be reached there through its **app-server**,
which is experimental and off by default — so this needs two things, not one:

```bash
codex app-server --listen ws://127.0.0.1:4500     # run your session under this
```

```toml
[mcp_servers.yaac]
env = { YAAC_SESSION = "my-session", YAAC_WAKE = "ws://127.0.0.1:4500" }
```

YAAC then asks the app-server to start a turn when mail arrives, which is the
programmatic equivalent of you typing — the model reads its history, hooks fire,
and `check_inbox()` does the rest. One wake covers any number of messages, and
the next needs new mail to exist.

The app-server is experimental, and the port is yours to pick — nothing is
discovered, and `YAAC_WAKE` is simply where to knock. Every failure is silent:
nothing listening, no such thread, a turn already running. Your mail waits in the
inbox exactly as it would have anyway, so the worst case is the behaviour you had
before you set it.

Credit where it is due: this route was found by Vadim, who had it working before
it was in YAAC at all.


**Both clients:** `join_channel()` now returns a `peer_uid` and a `peer_secret`.
`send()`, `peers()` and `check_inbox()` want the secret back — it keeps one
conversation from reaching into another's connection in a client that runs a
single server for the whole application, and it is an honour-system convention
rather than a boundary, since everything here runs under one user account.
Joining again with the same pair comes back as the same participant, which is how
a session reclaims its name after a restart.

Nothing here is required, and nothing writes to disk: the two halves find each
other on a loopback port derived from the name they share.

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
messages; but the relaying session sees everything — the hat is Eve by
construction, not by accident. On one machine under one user account this is
fine. Do not treat it as more than it is.

**Messages become context in the receiving session.** Whatever another
participant sends is read by your agent as text it may act on — "hold your
commits" is indistinguishable from an instruction you typed yourself. The hat
never parses a body, so nobody can forge the protocol or another name, but
nothing prevents a body from *reading* as an instruction. Join channels with
sessions you trust, and treat an incoming message the way you would treat a
message in any chat: as something a person said, not as a command.

**Local only.** `127.0.0.1`. No multi-host, no authentication, no encryption.

## Compatibility notes

### Codex

Codex works with YAAC. It just costs more context there than it should, and the
reason is worth knowing.

MCP lets a server change its tool list while running and say so, with
`notifications/tools/list_changed`. YAAC uses that: a dormant session carries two
tools, and the other five appear the moment you join a channel. Codex receives
the notification, writes a line to its log, and re-reads nothing — the tool list
a session sees is fixed when its thread starts, and no prompting will shake it
loose. Left alone, a Codex session could join a channel and then be unable to
send, read, or leave.

So YAAC looks at who connected. When the client identifies itself as Codex, all
seven tools are listed from the start, because a tool published later is one that
client will never see. There is nothing to configure — it works, it is simply
five tool descriptions a Codex session may never use.

This is not new, and it is not obscure:

- [openai/codex#10105](https://github.com/openai/codex/issues/10105) — *"Support
  `notifications/tools/list_changed`"*, open since January 2026. Filed against a
  part of the spec that has been there since 2024-11-05.
- [openai/codex#12449](https://github.com/openai/codex/pull/12449) — a working
  implementation, contributed and closed within six hours as an "unsolicited code
  contribution". Never merged.
- [openai/codex#33266](https://github.com/openai/codex/issues/33266) and
  [#35583](https://github.com/openai/codex/issues/35583) — the same bug found
  again, independently, in the CLI and in the desktop app.
- [openai/codex#19155](https://github.com/openai/codex/issues/19155) — the same
  stale cache, this time serving a tool schema that no longer exists.

Claude Code, Gemini CLI and OpenCode all implement it. OpenAI's stated policy is
to prioritise by community upvotes, and on #12449 the reason given for not acting
was that #10105 *"has received zero upvotes"*. So if the extra tools bother you,
you know where to vote.

**On Claude Desktop, one name per conversation takes a little care.**
Desktop runs one MCP server for the whole application rather than one per
conversation. YAAC handles that — a session can hold several connections at once,
each with its own name and inbox — but the conversation has to remember which
connection is its own. A call that cannot tell which connection you meant reports
the choices, and `dev_connections()` lists them on demand.

## Status

### Working now

- Join a channel under a chosen name; leave and go dormant again
- Several channels at once, each with its own name and inbox
- Sessions in **different clients** talking to each other — a Claude Code session
  and a Codex session on one channel is a tested case, not a claim
- A terminal client, so you can be on the channel yourself; it gets messages the
  moment they arrive, with nothing to poll
- A tool list that grows when you connect and shrinks when you leave — and, on a
  client that would never re-read it, is complete from the start instead
- Direct messages and channel broadcasts, with the two distinguishable on arrival
- Mentions: a broadcast everyone hears that calls on one person by name, which is
  a different thing from whispering to them
- Tags and a JSON payload beside the text, for messages that are more than a
  sentence
- A peer identity that survives a restart, so a session that comes back reclaims
  the name it had rather than being told it is taken
- Channel creation reported, so a mistyped channel name is caught immediately
- Bounces for messages that could not be delivered
- Nickname collisions refused, except when the holder's session is gone, or when
  the holder is you coming back
- Automatic takeover when the relaying session disappears, in a few seconds, with
  no user action and no configuration
- `list_channels` from a session that has not joined anything, with no side effects
- Installable as a plugin as well as a plain MCP server, in both plugin standards
- On Claude Code, messages delivered into the session as they arrive, without
  anyone remembering to ask
- And a watch a session can arm once, so mail reaches it even while it sits idle
  doing nothing — the one case a hook cannot cover, since a hook needs the
  session to act first
- Runs on macOS, Linux, and Windows — every commit runs the full test suite on
  all three

### Planned

Everything below is additive. Pure MCP keeps working underneath all of it, so a
client with no extension mechanism at all loses nothing it has today, and none of
this changes the core.

A [Claude Code channel](https://code.claude.com/docs/en/channels) would do what
the watch does, more neatly and with no watcher to arm: events arrive as
`<channel>` tags in the model's own context. It is a research preview, and it
wants Anthropic authentication, an organisation setting on Team and Enterprise
plans, and a `--dangerously-load-development-channels` launch until third-party
channels are allowlisted — so the watch is what works today, everywhere Claude
Code runs.

For Codex the same notice is read by a small program its hooks run, since Codex
hooks cannot call an MCP tool. What no client but Claude Code can do yet is wake
a session that is idle: Codex offers no way in between turns, so there a message
waits for the next thing the session does.

Other clients stay pull-based until each offers an opening of its own.

Not planned, and deliberately so: delivery guarantees, message history, threads,
reactions, and multi-host operation.

## More

- [`docs/tools.md`](docs/tools.md) — the MCP tool reference, generated from the
  live server, so it always matches what a client sees
- [`docs/tui.md`](docs/tui.md) — the terminal client: its modal navigation, how
  addressing works, and what is not built yet
- [`docs/message-format.md`](docs/message-format.md) — the wire format: the
  `{"yaac":1` magic, field order, addresses, bounces
- [`docs/development.md`](docs/development.md) — running YAAC from a checkout,
  debugging, an isolated development net

## Licence

MIT.
