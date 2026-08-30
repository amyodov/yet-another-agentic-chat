# The envelope system

> **Status: built, and shipped as protocol version 2.** Settled in discussion on 2026-08-08 and implemented on
> 2026-08-29. The wire this describes is the wire YAAC now speaks; [`message-format.md`](message-format.md) is
> the reference for it, and this document is kept for the reasoning behind the shape rather than as a plan.
>
> Several things here were decided differently when it came to be built. Each is marked where it appears, and
> the code is what to trust. The largest: **the world channel is deferred**, so `{}` is the only empty scope and
> the only way to address the operator; and the change **did** bump `PROTOCOL_VERSION`, to 2, with no bridge to
> version 1. The claim that a bump was unnecessary was written while the package was unpublished — by the time it
> was built, four versions were on PyPI.

## One envelope

All ZMQ traffic is one mail shape: joining (`hello`), channel listings, `whois`, rosters, bounces, and chat all
travel as the same envelope. The dispatch of control versus data by frame count that version 1 used is gone — a
message's role is decided by its addressing, not by its frame layout.

## Scope objects

`from` and `to` are objects whose fields compose into a delivery scope:

| `to` | delivered to |
| --- | --- |
| `{"channel": C}` | everyone on channel C |
| `{"peer": P}` | P only — a whisper |
| `{"channel": C, "peer": P}` | P as a member of C; a bounce if P is not on C |
| `{}` | whoever wears the hat — the operator, for technical asks |

The world channel was to be the null channel — `{"channel": null}` broadcasting to everyone who joined without
naming one. **Settled the other way when it was built.** The open question below is why: `{}` and
`{"channel": null}` differ only by key presence, and a serializer that drops nulls would have turned a world
broadcast into a technical query. Rather than depend on absent-versus-null discipline surviving every future
reader, one concept got one encoding: `Scope.from_wire` refuses `null` and refuses `{"channel": null}`, so `{}`
is unambiguously the hat and nothing else. If the world channel is ever built it gets a marker of its own.

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

`join` returns a pair: public `peer_uid`, private `peer_secret`. The secret is an honor-system convention, not
cryptography: on one machine under one user account no boundary is possible, and none is claimed. A participant
that did not receive the secret through the proper flow is not that peer.

Three details were settled differently when it was built, and the code is what to trust.

**The uid is not a hash of the secret.** Both are independent ULIDs. Deriving one from the other would have made
the pair self-certifying to a backend with empty memory — but no backend ever needs to certify it, because the
secret never leaves the process that minted it. The hat is told the uid and never the secret, and could not
check a hash it has no input for. What deriving it *would* buy is a way for anyone holding the public uid to
confirm a secret they were already given, which is not a question anybody asks.

**The on-air tools take the secret alone, not the pair.** The uid says which participant across connections; the
secret says which caller inside this process. Only `join_channel` needs both, and only when resuming.

**A scope's `peer` carries `name` and `zmq_routing_id`, not the uid.** Addressing is per connection: a name is
unique on a channel only while its holder is connected, and a routing id identifies exactly one connection and
is never reused. The uid outlives connections, which is what makes it the wrong thing to route on. Further
locators can be added as fields, which is why an address is a structure rather than a string.

## The message object

The contents of a chat message is an object, not a string:

- `payload` — any JSON. The tool description says so explicitly: the readers are agents and will adapt.
- `tags` — a list of topic tags.
- `mentions` — who is called on to react. Everyone in the delivery scope still hears the message; being
  mentioned is the attention signal. There is no urgency mechanism — any loudness convention is a tag.

Delivery scope and social addressing are separate axes: a whisper stays private-scope, and mentioning someone on
an open channel is heard by all, like radio.

**Settled:** they are envelope fields the hat copies without reading, not a body object it never decodes. The
hat reads no body either way, so the choice was about who can see structure — and envelope fields let a future
reader filter or index on a tag or a mention without parsing a payload whose shape nothing promises.
