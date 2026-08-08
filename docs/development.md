# Working on YAAC

Clone it and let `uv` run it from your checkout, so your edits take effect on the
client's next restart with no reinstall:

```bash
git clone https://github.com/amyodov/yet-another-agentic-chat
cd yet-another-agentic-chat
uv sync
uv run pytest        # ~25 s
uv run ruff check . && uv run ruff format .
```

Point a client at the checkout with `uv run --directory`:

```bash
claude mcp add yaac-dev -- \
  uv run --directory /path/to/yet-another-agentic-chat yaac \
  --endpoint tcp://127.0.0.1:19117
```

The `--endpoint` is worth adding while developing: it puts your working copy on a
separate rendezvous point, so a half-finished change cannot disturb the sessions
you have on the released build, and the two nets stay invisible to each other.

`19117` is just the port after the default. Any free port below 32768 will do;
staying under that range keeps the kernel from handing the same number out as the
source port of some unrelated outbound connection, which would make the bind fail
for reasons that have nothing to do with YAAC.

## Debugging

**YAAC writes no files at all.** Everything — unread messages, rosters, who is on
which channel — lives in memory and dies with the process. There is nothing to
clean up after a crash, nothing to leak between sessions, and nothing to find on
disk. Unread messages are lost if a session exits before collecting them, which is
consistent with a v0 that makes no delivery guarantees.

Each session logs to stderr, which your client will show as MCP server output:

```
[yaac] won the bind: this session is now wearing the hat on tcp://127.0.0.1:19116
[yaac] hello: 'Колян' on 'z combinator forum' as b'01JZ...'
[yaac] on air as 'Колян' on 'z combinator forum' (participant)
```

For the project's working rules — vocabulary, hard rules, test conventions — see
[`CLAUDE.md`](../CLAUDE.md). For the wire protocol, see
[`message-format.md`](message-format.md).
