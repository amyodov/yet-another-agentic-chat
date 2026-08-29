# The terminal client

> **Status: partly built.** The modal axis, live delivery, presence lines and recipient picking work
> (`src/yaac/chat.py`, `tests/test_chat.py`). Everything under *Not built yet* is agreed and unwritten.

```bash
uvx --from "yet-another-agentic-chat[chat]" yaac-chat
```

`textual` is an optional extra, so an MCP-only install stays at `pyzmq` and `mcp`. `yaac-chat` is a separate entry
point rather than a flag on the server, because the two have opposite invariants: the MCP server must write
nothing to stdout, since stdout is its transport, and this owns the screen. One binary that is sometimes a silent
pipe is one stray flag away from a session dying with a parse error.

## It is a participant, not a client of the server

The chat window uses `Backend` and `Membership` directly and never speaks MCP. That matters for one reason beyond
tidiness: **the README's central caveat does not apply here.** "Messages do not arrive on their own" is a fact
about MCP, which has no server-to-client message that reaches a model's context. A terminal has no such problem —
the window holds its own DEALER, the hat pushes to it, and `Membership.on_change` redraws. Nothing polls, and
nothing has to remember to check.

A side effect worth knowing: a chat window left open is a stable hat, so a net containing one stops changing hands
every time an agent session exits.

## One axis, three modes

Channels, chat, members, in that order — outward on the left, inward on the right. A single spatial rule, so
there is nothing to memorise:

| | empty prompt | text, cursor mid-line | text, cursor at end |
| --- | --- | --- | --- |
| **→** | members | move right | accept the suggestion |
| **←** | channels | move left | move left |
| **Tab** / **Shift-Tab** | next / previous mode | same | same |
| **Esc** | — | back to chat | back to chat |

The empty prompt is what frees the arrows: with nothing to move through, they cannot mean "move the cursor". Tab
carries no such guard, and that is the point of having it — the roster stays reachable mid-sentence, without
losing the draft.

Right doubles as accept-the-completion, the shell convention, which is what keeps Tab free for the modes. Tab
would otherwise be the completion key, and a Tab that sometimes completes and sometimes teleports is how a
terminal UI starts feeling unreliable.

## Addressing is a sticky choice, not a mode

The README promises direct-by-default and treats broadcast as a genuine announcement, so the interface has to make
the expensive act the deliberate one. The members list is therefore a picker with a consequence: choosing someone
makes them the recipient, and the recipient persists in the status line until changed. `everyone on the channel`
is the first row — something you choose, not the state you fall into by saying nothing.

If your recipient leaves, addressing falls back to the channel and says so, rather than quietly sending a whisper
into a routing id that is gone.

## No commands, because names cannot be parsed

Selecting a channel borrows the prompt for one question — *join z combinator forum as…* — and Enter joins.
Selecting `＋ join a channel…` asks twice, for the channel and then the name. Esc abandons the question and leaves
you where you were; while one is pending the arrows stay put, so a half-answered question cannot be navigated away
from.

This is not a style preference. Channel and participant names are arbitrary UTF-8 that may contain spaces, and
hard rule 4 says they are never parsed or split. A command like `/join <channel> as <name>` could only work by
splitting on `" as "` — which is parsing a name, and which cannot address a channel actually called `as`, or one
containing it. One question, one whole line, no grammar. Only leading and trailing whitespace is stripped;
everything else the person typed is the name.

## Presence lives in the transcript

A single column cannot dock a roster panel, and a mode you have to *enter* cannot answer "who is here" while you
are reading. So presence is written into the history, the way single-column chats have always done it:

```
14:02  → Bob is here
14:03  Bob → you   holding my commits
14:41  ← Bob left
```

This is push, not polling: `hat.broadcast_roster()` sends the full member list to every member of a channel on
each `hello` and each eviction, so the client only has to diff what it is handed.

Two honest limits. **Departures are late** — eviction is lazy, discovered on the first failed send, so a session
that died silently lingers until someone writes to it. And the roster shows who is *present*, not who is *awake*:
with no heartbeat, an idle net generates no traffic and there is nothing to distinguish a working session from an
abandoned one. There is no green dot because we have not earned one.

The status line carries the same roster by name rather than by count — a YAAC channel holds a handful of sessions,
not a crowd, so the names fit and are more useful than a number.

## Not built yet

- **Several memberships at once.** `Backend` already holds a dict of them, so the protocol needs nothing; the
  client keeps one, and switching channels leaves the old one.
- **`@name` completion.** Ghost text on the prompt, with an up/down candidate strip when a prefix is ambiguous.
  Up/Down are displaced from history recall only while that strip is showing.
- **Last-traffic times** (`5m`) in the members list. The hat has the timestamps; the roster message would need to
  carry them. That is now an addition rather than a redesign -- version 2 puts a roster in the same envelope as
  everything else, and its `payload` is the hat's to shape.
  Call it "last message", never "last seen" — the healthiest participant in this system is one that has been
  quietly working for two hours.
- **Reachability on demand.** The hat can test the roster by attempting a send and reporting who raised
  `EHOSTUNREACH`. Real presence, paid for only when a human asks, which keeps "an idle net generates zero traffic"
  intact.
- **Scrollback affordances**: a new-messages marker when scrolled up, and search.
