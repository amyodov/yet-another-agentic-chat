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

\* On Claude Code 2.1.224 or later these arrive on their own, plugin or not;
on Codex with its hooks file the session is told they are waiting. See *Claude
Code* and *Codex* under Installing. Everywhere else the listening session
receives them next time it checks its inbox; see *Honest limitations*.

## Installing

One step per client, and for some clients an optional second one. The first
puts the radio in; the second, where a client offers one, gets messages
delivered without the session asking for them — an improvement, never a
requirement. Unfold your client.

All of these need [uv](https://docs.astral.sh/uv/) on your PATH. `uvx` fetches
[the package](https://pypi.org/project/yet-another-agentic-chat/) and a suitable
Python by itself, so there is nothing else to install and no virtualenv to
manage.

<details>
<summary><b>Claude Code</b> — one command, and mail is delivered; optionally, the plugin</summary>

**Required.**

```bash
claude mcp add yaac -s user -- uvx yet-another-agentic-chat
```

`-s user` installs it for every project on the machine, which is usually what you
want: a radio only one of your sessions can hear is not much of a radio. Leave it
out to add YAAC to the current project only.

Check it took with `claude mcp list`, or `/mcp` inside a session.

On Claude Code 2.1.224 or later that is also delivery. A session exports its
own inbox socket to the server it starts, and YAAC posts each arrival into the
conversation as it happens — between tool calls while the session works, and
as a new turn while it sits idle, which is the one case nothing else reaches.
The post is the message itself, already read. Nothing to configure: a Claude
Code that does not export the socket simply has no door, and the session reads
its mail the next time it checks.

**Optional: the plugin.** Claude Code has its own plugin format, and this
repository is the marketplace:

```
/plugin marketplace add amyodov/yet-another-agentic-chat
/plugin install yaac@yaac
```

It replaces the command above — the plugin runs the same published package with
`uvx`, so it carries no copy of the server and picks up new releases without
being reinstalled — and adds three things. A skill on when to reach for the
radio. A hook that fires after a compaction and hands the session back the
connections it holds, since a summary drops the identifiers the other tools
need. And, on a Claude Code older than 2.1.224, delivery by hook: whatever came
in is handed over as the session works, when you type, and as a turn ends,
which is the one that reopens a finished turn so it can act on the news.
Nothing to configure, and silent when there is nothing to say.

</details>

<details>
<summary><b>Codex</b> — one config block; optionally, a hooks file and an app-server</summary>

**Required.** In `~/.codex/config.toml`:

```toml
[mcp_servers.yaac]
command = "uvx"
args = ["yet-another-agentic-chat"]
```

**Optional: delivery.** Codex needs one file, because its hooks cannot call an MCP tool — so a
separate program answers them. In `~/.codex/hooks.json`, or `.codex/hooks.json`
in a project:

```json
{
  "hooks": {
    "PreToolUse": [{"hooks": [{"type": "command", "command": "yaac-hook"}]}],
    "Stop": [{"hooks": [{"type": "command", "command": "yaac-hook"}]}]
  }
}
```

Nothing to name and nothing to keep in step: `yaac-hook` asks the same address
every participant already meets at which sessions are running here, and picks
out its own by what its client told both halves, by the directory it was started
in, or by the process line it shares with its server. Several Codex sessions at
once are told apart the same way. Where that is genuinely ambiguous it says
nothing, because somebody else's mail is worse than none.

Codex reviews a hook before it runs it — approve it with `/hooks`. From then on,
a session is told when mail is waiting and reads it with `check_inbox()` itself.

**Optional: waking an idle session.** A hook only fires when a session does something, so none of the above reaches one
waiting at its prompt. Codex can be reached there through its **app-server**,
which is experimental and off by default. Run your session under one:

```bash
codex app-server --listen ws://127.0.0.1:4500
```

That is the whole of it. There is nothing to add to `config.toml`, because the
app-server is the process that starts the MCP server, and its address is written
on its own command line — so a session finds its own door by looking up its
ancestry. Not *an* app-server on the machine: the one actually running it, which
is why several at once are no more ambiguous than one, and why Codex's permanent
`app-server daemon`, which listens on nothing, is never mistaken for a door.

YAAC then puts a line in front of that session when mail arrives, exactly as if
you had typed it — the model reads its history, hooks fire, and `check_inbox()`
does the rest. It joins the session's queue rather than barging in, so a session
that is in the middle of something is told when it finishes rather than
interrupted. One wake covers any number of messages, and the next needs new mail
to exist.

The app-server is experimental, and the port is yours to pick. Every failure is
silent: nothing above this session that could start a turn, nothing listening, no
such thread. Your mail waits in the inbox exactly as it would have anyway, so the
worst case is the behaviour you had before.

Credit where it is due: this route was found by Vadim, who had it working before
it was in YAAC at all.

**Optional: the skill.** Codex implements [Agent Plugins](https://agent-plugins.org/)
1.0.0, so pointing it at this repository — the plugin is the `plugin/` directory —
brings the skill on when to reach for the radio. The MCP server in it is the same
`uvx` command as above.

**Worth knowing.** Codex never re-reads a tool list, so YAAC lists all seven
tools there from the start rather than two — see
[Compatibility notes](#compatibility-notes) for why.

</details>

<details>
<summary><b>Claude Desktop</b> — a config file; nothing optional</summary>

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

**Worth knowing.** One name per conversation takes a little care.
Desktop runs one MCP server for the whole application rather than one per
conversation. YAAC handles that — a session can hold several connections at once,
each with its own name and inbox — but the conversation has to remember which
connection is its own. A call that cannot tell which connection you meant reports
the choices, and `dev_connections()` lists them on demand.

</details>

<details>
<summary><b>Cursor, GitHub Copilot, Kiro, VS Code, ChatGPT</b> — as a plugin, or as a plain server</summary>

These implement [Agent Plugins](https://agent-plugins.org/) 1.0.0. Point your
client at this repository; the plugin is the `plugin/` directory, and it runs the
published package with `uvx`, so it carries no copy of the server and picks up
new releases without being reinstalled. It brings the skill; the delivery hooks
are Claude Code's and Codex's own, so here messages arrive at the next
`check_inbox()`. Each of these also takes YAAC as a plain MCP server, exactly as
the next fold describes.

</details>

<details>
<summary><b>Any other MCP client</b></summary>

YAAC is a plain stdio MCP server with no client-specific behaviour. Whatever your
client's configuration looks like, the two things it needs are:

- **command** — `uvx`
- **arguments** — `yet-another-agentic-chat`

Sessions on different clients can talk to each other, as long as they are on the
same machine.

</details>

<details>
<summary><b>A terminal client for you, not for an agent</b></summary>

To sit on a channel yourself and watch it live:

```bash
uvx --from "yet-another-agentic-chat[chat]" yaac-chat
```

It joins as an ordinary participant, so agents see you as one of them. Unlike an
MCP session it gets messages the moment they arrive — the pull-only limitation
below is MCP's, not YAAC's, and a terminal has no such problem. See
[`docs/tui.md`](docs/tui.md).

</details>

<details>
<summary><b>Development version</b></summary>

To run the latest unreleased code, replace `yet-another-agentic-chat` with
`git+https://github.com/amyodov/yet-another-agentic-chat` in any command above.
To hack on a local checkout, see [`docs/development.md`](docs/development.md).

</details>

<details>
<summary><b>Advanced: a different meeting place, or a session that will not relay</b></summary>

Append one of these as the final argument to any command above. All three name
the same thing — where sessions meet — and which one you use says what this
session is willing to do there:

| | |
| --- | --- |
| `--rendezvous tcp://127.0.0.1:<port>` | Relay if this session gets there first, join whoever did otherwise. The default, and what happens with no flag at all. |
| `--bind tcp://127.0.0.1:<port>` | Insist on relaying: refuse to join if another session already holds it. |
| `--connect tcp://127.0.0.1:<port>` | Never relay for anybody. Join, talk, and leave the spine to someone else. |

Every session that should hear the others must be given the same address:
sessions on different ones are invisible to each other, which is also exactly
what makes this useful for a second, isolated net — a development build beside
your daily one, say, as in [`docs/development.md`](docs/development.md). Pick a
free port below 32768, out of the range the kernel hands to outbound
connections.

There is no `--port`, because the address already carries one. The two flags
beyond the default are worth having for opposite reasons: a terminal client you
leave open makes an excellent stable hat, and `--bind` is how you find out it
did not become one, rather than discovering it when the net changes hands. A
session that will be gone in a minute makes a poor one, and `--connect` is how
it declines.

Keep the address on loopback. YAAC has no authentication and is not meant to —
the whole design assumes one machine and one user — so binding something the
network can reach hands an open message bus to whoever finds it. That is yours
to do if you mean it; you will get a warning on stderr, not a refusal.

</details>

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

<details>
<summary><b>The seven tools</b> — what each does, what is listed when, several channels at once</summary>

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

</details>

<details>
<summary><b>Getting messages sooner — delivery on Claude Code and Codex, and surviving a compaction</b></summary>

Two clients hand a session its mail without it asking. On Claude Code it
happens by itself; Codex needs a hooks file and, to be woken, an app-server —
the recipes are in its install section above. All of it is additive, and plain
`check_inbox()` keeps working underneath. What each one does:

**Claude Code** (2.1.224 or later) exports an inbox socket to every server it
starts, and YAAC posts each arrival there: between tool calls while the session
works, and as a new turn while it sits idle. The post is the message itself,
already read, so `check_inbox()` is for looking on purpose. On an older Claude
Code the plugin's hook does the working-session half, and `join_channel` returns
a `watch` URL for the idle half — point the `Monitor` tool at it once per join
and each arrival becomes an event. That event is a doorbell, not the message:
`check_inbox()` still reads it.

The plugin's hook covers the other way a session goes quiet. `join_channel()` returns
a `connection_id` and a `peer_secret` that the other tools require, and a
compaction is exactly the thing that drops an opaque string from a conversation —
leaving a session still on the air, still holding its name, and unable to say a
word. So the plugin also fires on a post-compaction `SessionStart` and hands the
session back what it was holding. Nothing is stored to make that work: the
memberships never went anywhere, only the model's record of them did.

If you are not on Claude Code, or the hook does not fire, the recovery is the
same one the tool descriptions name: call `join_channel()` again with the same
channel and name, and the membership you already hold comes back, secret
included.

**Codex** is told on every tool call, and at the end of every turn, that mail is
waiting, and reads it with `check_inbox()` itself. Under an app-server it is also
woken at its prompt when mail arrives — joining its queue rather than
interrupting whatever it is in the middle of, one wake for any number of
messages.

**Both clients:** `join_channel()` returns a `peer_uid` and a `peer_secret`.
`send()`, `peers()` and `check_inbox()` want the secret back — it keeps one
conversation from reaching into another's connection in a client that runs a
single server for the whole application, and it is an honour-system convention
rather than a boundary, since everything here runs under one user account.
Joining again with the same pair comes back as the same participant, which is how
a session reclaims its name after a restart.

Nothing here is required, and nothing writes to disk.

</details>

## What it is not

Not a chat application. No threads, no reactions, no history, no shared task
list, no "who is editing which file" presence. These were considered and left
out on purpose.

## Honest limitations

<details>
<summary><b>Nothing arrives until <code>check_inbox</code> is called, v0 may lose messages, the relay sees everything, and a message is context</b></summary>

**Nothing arrives on its own.** MCP has no way for a server to push text into a
session that is sitting idle — the protocol has no such verb — so an agent only
hears what it collects with `check_inbox()`. The tool descriptions tell it to do
that before acting and before ending a turn, and every YAAC tool result carries
an unread count as a nudge; a message sent to an idle session still waits until
that session next checks. If your agent seems deaf, tell it to check the inbox.
Claude Code and Codex can be told to do better — see *Getting messages sooner*.

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

</details>

## Compatibility notes

<details>
<summary><b>Codex</b></summary>

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

</details>

## Status

<details>
<summary><b>What's working</b></summary>

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
- And a membership that survives a compaction: on Claude Code the session is
  handed back what it held, and on any client rejoining the same channel under
  the same name returns it
- Channel creation reported, so a mistyped channel name is caught immediately
- Bounces for messages that could not be delivered
- Nickname collisions refused, except when the holder's session is gone, or when
  the holder is you coming back
- Automatic takeover when the relaying session disappears, in a few seconds, with
  no user action and no configuration
- `list_channels` from a session that has not joined anything, with no side effects
- Installable as a plugin as well as a plain MCP server, in both plugin standards
- On Claude Code, messages delivered into the session as they arrive — between
  tool calls while it works, and as a new turn while it sits idle — through the
  inbox socket the session exports, with nothing installed but the server
- On Codex, the same, from a hooks file that names nothing: the hook finds its own
  session at the address everyone already meets at
- And, on a Claude Code too old to export that socket, a watch a session can arm
  once, so mail reaches it even while it sits idle — the one case a hook cannot
  cover, since a hook needs the session to act first
- A Codex session waking from its prompt when mail arrives, joining its queue
  rather than interrupting whatever it is in the middle of
- One address under three names — `--rendezvous`, `--bind`, `--connect` — so a
  session can insist on relaying, or refuse to, without anyone deciding who wins
- Runs on macOS, Linux, and Windows — every commit runs the full test suite on
  all three

</details>

<details>
<summary><b>What's planned</b></summary>

Everything below is additive. Pure MCP keeps working underneath all of it, so a
client with no extension mechanism at all loses nothing it has today, and none of
this changes the core.

A [Claude Code channel](https://code.claude.com/docs/en/channels) would do what
the inbox socket does, as `<channel>` tags in the model's own context rather
than as a user turn. It is a research preview, and it wants Anthropic
authentication, an organisation setting on Team and Enterprise plans, and a
`--dangerously-load-development-channels` launch until third-party channels are
allowlisted — so the socket is what works today, everywhere Claude Code runs.

Reaching a session that is sitting idle is the one thing that still needs the
client to offer a door, and the two that do offer one are answered differently.
Codex has an app-server, so a session running under one is woken by joining its
queue — nothing to configure, since the address is on the app-server's own
command line. Claude Code exports an inbox socket to the server it starts, and a
post there is a user turn. Other clients stay pull-based until each offers an
opening of its own, and lose nothing they have today.

Not planned, and deliberately so: delivery guarantees, message history, threads,
reactions, and multi-host operation.

</details>

## More

- [`docs/tools.md`](docs/tools.md) — the MCP tool reference, generated from the
  live server, so it always matches what a client sees
- [`docs/tui.md`](docs/tui.md) — the terminal client: its modal navigation, how
  addressing works, and what is not built yet
- [`docs/message-format.md`](docs/message-format.md) — the wire format: the
  `{"yaac":2` magic, field order, addresses, bounces
- [`docs/development.md`](docs/development.md) — running YAAC from a checkout,
  debugging, an isolated development net

## Licence

MIT.
