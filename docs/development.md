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

## Working on YAAC from Codex

`.codex/hooks.json` in this repository wires the delivery hooks for a Codex
session opened here, so working on YAAC means dogfooding it. It runs
`uv run yaac-hook --key yaac-dev`, which only finds anything if the server it
talks to was launched with the same name:

```toml
[mcp_servers.yaac]
command = "uv"
args = ["run", "--directory", "/path/to/yet-another-agentic-chat", "yaac"]
env = { YAAC_SESSION = "yaac-dev" }
```

Codex asks you to trust a hook before running it (`/hooks`), and re-asks when
the file changes, so nothing here runs behind your back. The user-facing version
of this setup, with the published package rather than the working tree, is under
*Getting messages sooner* in the README.

## Publishing

The release itself is a checklist, kept as the `releasing` skill in
`.claude/skills/` so it runs the same way every time: preconditions, version
bump, plugin manifests regenerated, tag, GitHub release, and the trusted
publisher takes it from there. This section covers the surrounding chores that
are not part of that run.

Nothing here needs a secret committed anywhere. `CONTEXT7_API_KEY` lives in your
shell; the registry and PyPI both authenticate interactively.

### The generated files

Two things in the tree are written by scripts and will fail the suite if edited
by hand or left stale:

```bash
uv run python .claude/skills/updating-mcp-tools/scripts/generate.py      # docs/tools.md
uv run python .claude/skills/releasing/scripts/generate_manifests.py # the plugin manifests
```

The first reads the live tool registry, so the documented tools cannot drift
from what a client is served. The second writes the plugin and registry manifests from
`pyproject.toml`, each of which restates the version.

### Nudging Context7

Context7 re-crawls a project of this size roughly every 45 days, so without a
push its answers describe the previous release for weeks:

```bash
curl -sS -X POST https://context7.com/api/v1/refresh \
  -H "Authorization: Bearer $CONTEXT7_API_KEY" -H "Content-Type: application/json" \
  -d '{"libraryName": "/amyodov/yet-another-agentic-chat"}'
```

The key comes from [context7.com/dashboard](https://context7.com/dashboard). The field is `libraryName`, and the
body has to be JSON — a form post, or the `libraryId` name the API guide implies, returns a bare 500. Refreshes
are capped at one per 10 days, so `{"error":"too-early"}` means the index is already recent enough rather than
that anything went wrong.
Worth remembering that the `rules` in `context7.json` are printed verbatim ahead
of every answer Context7 gives about YAAC — a stale one is wrong in public and
invisible from here.

`url` and `public_key` in the same file are the ownership claim, and the key is
public by design: Context7 proves the claim by fetching the file over HTTP from
the repository it is claiming, so a secret would be no evidence of anything.
Claiming is done once, in the modal on the
[library's Context7 page](https://context7.com/amyodov/yet-another-agentic-chat),
by pasting the raw URL of the committed file:

```
https://raw.githubusercontent.com/amyodov/yet-another-agentic-chat/main/context7.json
```

What Context7 indexes is everything the file does not exclude, which is the
README, `docs/` and the bundled skill. `src/` and `tests/` are out because the
tool reference is generated from the running server and says more than a reader
of the source would extract; `CLAUDE.md` is out because it is instructions for
working on YAAC rather than for using it; and `docs/zmq.md` is out because it
specifies a protocol that does not exist yet, which is the one document capable
of sending an agent to build against the wrong wire.

`.claude/` and `.github/` are excluded for the same reason as `CLAUDE.md`, and
they stay excluded whatever the crawler does. Measured today, they were never
reaching the index in the first place: a question only the releasing skill can
answer returns nothing, and a question about the CI matrix is answered out of the
README rather than `ci.yml`. That is not a reason to drop the entries -- the
crawler's treatment of dot-directories is undocumented and not ours to rely on,
and two strings buy a guarantee that does not depend on it.

### The MCP registry

[registry.modelcontextprotocol.io](https://registry.modelcontextprotocol.io) is
the authoritative index of public MCP servers, and Smithery, PulseMCP, Docker
Hub and others consume it. It has no search box; it is an API:

```bash
curl -s "https://registry.modelcontextprotocol.io/v0/servers?search=yaac&limit=10"
```

Publishing uses `mcp-publisher`, a Go binary — no npm or PyPI package, so it is
not a dependency this project can declare:

```bash
brew install mcp-publisher

mcp-publisher login github     # OAuth device flow, once
mcp-publisher validate
mcp-publisher publish
```

Only needed by hand for a first listing or a fix: `publish.yml` republishes on
every release, authenticating with `github-oidc` rather than a stored token, so
the entry follows the version without anyone logging in.

Without Homebrew, the releases carry per-platform tarballs; fetch one to a
temporary directory and run it from there rather than adding anything to the
checkout.

`server.json` is generated alongside the plugin manifests, so its version tracks
the release rather than being typed in. The name is
`io.github.amyodov/yet-another-agentic-chat`: GitHub authentication grants
`io.github.<user>/*`, and an organisation namespace needs Owner rights on that
organisation rather than mere membership. The registry caps `description` at 100
characters where PyPI does not, which is why that one line is written separately
from the project's own.

**The ordering trap:** the registry proves ownership of a PyPI package by
finding an `mcp-name:` string in the package *description* — that is, in the
README of the published artifact, not the one in git. The marker sits near the
top of `README.md` in an HTML comment. A submission can only verify once a
release carrying it is on PyPI, so the release comes first and the registry
second.

For the project's working rules — vocabulary, hard rules, test conventions — see
[`CLAUDE.md`](../CLAUDE.md). For the wire protocol, see
[`message-format.md`](message-format.md).
