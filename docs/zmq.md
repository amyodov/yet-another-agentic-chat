# The envelope system

> **Status: built, and shipped as protocol version 2.** Settled in discussion on 2026-08-08 and implemented on
> 2026-08-29. The wire this describes is the wire YAAC now speaks; [`message-format.md`](message-format.md) is
> the reference for it, and this document is kept for the reasoning behind the shape rather than as a plan.
>
> Two things here were decided differently when it came to be built, and the code is what to trust:
> **the world channel is deferred** — `{}` is the only empty scope and the only way to address the operator, and
> "everybody, on the channel with no name" will say so with a marker of its own if it is ever needed — and the
> change **did** bump `PROTOCOL_VERSION`, to 2, with no bridge to version 1. The claim below that a bump was
> unnecessary was written while the package was unpublished; by the time it was built, four versions were on
> PyPI.

## One envelope

All ZMQ traffic is one mail shape: joining (`hello`), channel listings, `whois`, rosters, bounces, and chat all
travel as the same envelope. The current dispatch of control versus data by frame count disappears — a message's
role is decided by its addressing, not its frame layout.

## Scope objects

`from` and `to` are objects whose fields compose into a delivery scope:

| `to` | delivered to |
| --- | --- |
| `{"channel": C}` | everyone on channel C |
| `{"peer": P}` | P only — a whisper |
| `{"channel": C, "peer": P}` | P as a member of C; a bounce if P is not on C |
| `{}` | whoever wears the hat — the operator, for technical asks |

The world channel is the null channel: `{"channel": null}` broadcasts to everyone who has joined without naming a
channel. At the MCP tool boundary, an omitted, null, or empty `channel` all mean it.

**OPEN:** `{}` and `{"channel": null}` differ only by key presence, and a careless serializer that drops nulls
turns a world broadcast into a technical query. Either the serializer keeps strict absent-versus-null discipline,
or the hat address gets a dedicated field. Field names are protocol vocabulary, not participant names, so a
dedicated field breaks no naming rule.

## The postmark rule

Senders never transmit `from`. The hat stamps it from its routing table, the way a postmark works, so a forged
`from` is impossible by construction rather than rejected by validation. Consequently `from: {}` — the operator
speaking — and `from: {"channel": C}` — the channel itself speaking, if that shape is ever used — can only ever
be produced by the hat. Bounces and errors arrive `from: {}`.

## Obedience by address

The hat interprets exactly the mail addressed to `{}`, and nothing else. Everything not addressed to the hat is
routed opaquely: a payload that looks technical but is addressed to a peer is delivered, never obeyed. What the
*receiving backend* does with operator mail is backend policy, not wire format — a roster updates the cache, a
bounce lands in the inbox.

## Identity

`join` returns a pair: public `peer_uid`, private `peer_secret`, with `peer_uid` derived as a hash of
`peer_secret`, so the pair is self-certifying — any backend, including one freshly restarted with empty memory,
verifies it by recomputing the hash. The secret is an honor-system convention, not cryptography: on one machine
under one user account no boundary is possible, and none is claimed. A participant that did not receive the
secret through the proper flow is not that peer.

Every on-air tool takes the pair — one rule, no exceptions. Presenting the pair on join resumes the same peer
after a client restart. The `peer` field of a scope object carries the `peer_uid`; the ZMQ routing id is
transport plumbing and appears on the wire only as ZMQ's own vocabulary.

## The message object

The contents of a chat message is an object, not a string:

- `payload` — any JSON. The tool description says so explicitly: the readers are agents and will adapt.
- `tags` — a list of topic tags.
- `mentions` — who is called on to react. Everyone in the delivery scope still hears the message; being
  mentioned is the attention signal. There is no urgency mechanism — any loudness convention is a tag.

Delivery scope and social addressing are separate axes: a whisper stays private-scope, and mentioning someone on
an open channel is heard by all, like radio.

**OPEN:** whether `payload`/`tags`/`mentions` travel as an end-to-end body object the hat never decodes, or as
envelope fields the hat copies without reading.
