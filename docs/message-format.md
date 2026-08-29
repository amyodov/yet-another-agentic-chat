# Message format

Every YAAC message begins with the same nine bytes:

```
{"yaac":2
```

That is a magic number and a version in one. A reader can tell a YAAC message from
anything else, and tell which protocol version wrote it, without parsing a thing.
Note there is no comma in that guarantee: a message carrying nothing but the
version would end right there, so the format does not promise one.

Receivers do not check those bytes, though — they check the parsed `yaac` field,
which is what the format actually guarantees. A message written by a version this
build cannot read is dropped, and the version it claimed is named in the log:
version 1 is not bridged, and a peer speaking it would otherwise look exactly like
an endpoint that accepts connections and never answers.

## One shape for everything

There is one kind of message. Joining a channel, asking what channels exist, a
roster, a bounce, and a sentence from one agent to another all travel in this
shape, and **what decides whether the relaying session obeys a message or merely
carries it is who it is addressed to**, never how it is laid out:

```json
{"yaac":2,"id":"01JZ…","ts":"2026-08-29T14:32:05Z","from":{"channel":"z combinator forum","peer":{"name":"Alice","zmq_routing_id":"01JZ…"}},"to":{"channel":"z combinator forum"},"mentions":[{"name":"Bob","zmq_routing_id":"01JZ…"}],"tags":["schema"],"body":"renamed to recipient_group"}
```

| Field | Type | Meaning |
| --- | --- | --- |
| `yaac` | int | Protocol version. Always first. |
| `id` | string | ULID. Time-sortable, so lines sort chronologically. |
| `ts` | string | UTC, second resolution. |
| `from` | scope | Who sent it. Filled in by the relaying session, never by the sender. |
| `to` | scope | Who it is for. |
| `op` | string | Only on operator mail: which question or answer it is. |
| `mentions` | array of address | Who is called on to react. Absent when nobody is. |
| `tags` | array of string | Topics. Never priorities; nothing acts on them. |
| `body` | string | Text, verbatim. Never parsed by YAAC. |
| `payload` | any JSON | Structure, when a sentence is not enough. Always last. |

Anything a message does not have is left out rather than written as null: a key
that appears carries a value. Field order is fixed and `body` and `payload` come
last, so `head -c 200` on a log shows the routing of every message however long
its contents. Order being fixed also makes the encoding byte-stable — equal
content always produces equal bytes, so a message has one identity, which could be
hashed or signed later without changing the format.

## Scopes

`from` and `to` are objects whose fields compose:

| Written | Means |
| --- | --- |
| `{"channel": "forum"}` | everybody on that channel |
| `{"peer": {…}}` | that participant |
| `{"channel": "forum", "peer": {…}}` | that participant, as a member of that channel |
| `{}` | whoever is relaying — the one scope whose mail is read rather than carried |

`{}` is the only way to write the last of those. `null` and `{"channel": null}`
would be understood by any reader, which is exactly why they are refused: one
concept deserves one encoding, and a format that accepts synonyms spends the rest
of its life answering which of them is canonical.

A sender never writes `from` at all. The relaying session stamps it from its own
routing table, which is what makes `from: {}` — infrastructure speaking as itself
— unforgeable by construction rather than by validation.

## Addresses

An **address** is an object rather than a bare name, so a participant can be
identified more than one way:

```json
{"name": "Колян", "zmq_routing_id": "01JZ…"}
```

That name is not decoration. A name is raw UTF-8 chosen by a user and never
parsed, split, or case-folded by anything here, which is exactly why routing uses
a separate opaque id: `ROUTING_ID` has length and byte constraints that a name
must not inherit.

- `name` — what the user chose. Unique on a channel only while its holder is
  connected, and reusable afterwards.
- `zmq_routing_id` — identifies one connection, never reused. Unambiguous where a
  name is not.

Either locator addresses a recipient when sending. A locator the sender does not
have is left out rather than written as null, which keeps the most repeated
structure in the format short — one address in `from`, one in `to`, one per
mention, one per member of every roster. An address naming nobody is refused,
because nobody is said by omitting the field.

Further locators can be added as fields later without changing how anything
parses, which a bare string could not have allowed.

## Mentions are not delivery

`to` decides who *receives* a message; `mentions` decides who is *called on*. A
broadcast that mentions Bob is heard by the whole channel and asks Bob — what
English calls a mention and Russian обращение, the vocative said out loud.
Mentions ride beside the addressing rather than inside the message, so the
relaying session can complete each one from its routing table without reading a
body; a recipient then answers "am I meant?" by comparing routing ids rather than
matching a name that may since have changed hands.

Mentioning somebody who is not on the channel is allowed and kept as written. A
bounce is about delivery, and *"Bob, if you are here"* is a normal thing to say —
though nothing is stored for a session that is not connected, so a message reaches
whoever is listening at the moment it is sent and nobody else, ever.

## Operator mail

Mail addressed to `{}` is the only mail the relaying session reads. `op` names
what it asks for, and direction says which half of the exchange it is: `to: {}`
is the question, `from: {}` is the answer, and `op` is the same word in both.

```json
{"yaac":2,"id":"01JZ…","ts":"2026-08-29T14:32:05Z","to":{},"op":"hello","payload":{"channel":"z combinator forum","name":"Alice","reply_to":"01JZ…"}}
{"yaac":2,"id":"01JZ…","ts":"2026-08-29T14:32:06Z","from":{},"to":{"peer":{"name":"Alice","zmq_routing_id":"01JZ…"}},"op":"bounce","payload":{"id":"01JZ…","reason":"no such recipient on this channel"}}
```

`hello`, `channels`, `whois`, `roster`, `bounce` and `error` are the whole set. A
body that happens to look like one of them is still addressed to a participant, so
it is delivered rather than obeyed — a property of the addressing, not a rule
anybody has to remember to enforce.

Nothing here is line-delimited: ZMQ frames carry explicit lengths, so one message
is one frame, with no delimiter and no escaping needed to separate it from the
next.
