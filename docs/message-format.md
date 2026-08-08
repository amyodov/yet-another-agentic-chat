# Message format

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
