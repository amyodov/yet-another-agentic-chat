---
name: releasing
description: Cut a YAAC release - preconditions, version bump, regenerated manifests, git tag, GitHub release, and the indexes that follow it: PyPI, the MCP registry, Context7. Invoked by the user only; a release never happens on the assistant's own initiative.
argument-hint: [version]
disable-model-invocation: true
---

# Release a version of YAAC

One release reaches PyPI, the git tag and GitHub release, the official MCP registry, and Context7.
Releases only happen when the user asks for one — `disable-model-invocation` enforces that, and nothing in this
file overrides it. The version being released is `$1` (e.g. `0.1.1`): a bare semver, no leading `v` — the tag adds it.

The publish pipeline is: GitHub release → `.github/workflows/publish.yml` → PyPI trusted publisher (OIDC, no
tokens). This skill's job is everything around that trigger, in order, aborting loudly at the first failure
rather than improvising past it.

## 1. Preconditions

- Working tree clean, on `main`, in sync with `origin/main` (`git status -sb`). Unpushed work either goes into
  the release consciously or waits — it must not ride along by accident.
- `$1` is not already released: absent from `git tag` and from
  `https://pypi.org/pypi/yet-another-agentic-chat/json`.
- The full local suite passes: `uv run pytest`.
- CI is green on HEAD for all three OSes: `gh run list --branch main --limit 1`. Local tests cannot vouch for
  Windows; only the matrix can.

## 2. Version bump

Set `version = "$1"` in `pyproject.toml`, run `uv sync` so `uv.lock` follows, then regenerate the plugin
manifests, which carry the version too:

```bash
uv run python ${CLAUDE_SKILL_DIR}/scripts/generate_manifests.py
```

Commit all of it together. The version in
the code and the version being tagged must be the same string — a wheel that reports a different version than
its tag is a support puzzle nobody needs.

## 3. Tag and release

Push the bump first, then **wait for its own CI run to go green before tagging**. The green checked in step 1 was
for the commit before the bump; pushing starts a fresh run, and the commit being released is the one nobody has
verified yet. A red Windows job has hidden here before.

```bash
git push origin main
until [ "$(gh run list --branch main --limit 1 --json status --jq '.[0].status')" = completed ]; do sleep 20; done
gh run list --branch main --limit 1 --json headSha,conclusion --jq '.[0] | "\(.headSha[0:7]) \(.conclusion)"'
```

That has to print the bump commit and `success`. If it does not, stop: the tag is the point of no return.

```bash
git tag v$1 && git push origin v$1
gh release create v$1 --title "v$1 — <short summary>" --notes "<what changed and why it matters>"
```

Release notes follow the prose rules in CLAUDE.md: what changed and what it displaces, not a file list.
Creating the release is the publish trigger — after this line, the release is happening.

## 4. Watch and verify

- Watch the run: `gh run watch $(gh run list --workflow publish.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status`.
- Confirm PyPI serves the new version: `curl -s https://pypi.org/pypi/yet-another-agentic-chat/json` →
  `info.version == "$1"`.
- Cold-run the real user path: `uvx --refresh --from yet-another-agentic-chat yaac --help`.

## 5. Tell the indexes

Both of these describe a version, and both keep describing the previous one until told otherwise. Run them only
after step 4 confirms PyPI is serving `$1` — each reads the published package, so running them early records the
release that is still live.

**The MCP registry publishes itself.** `publish.yml` waits for PyPI to serve `$1`, authenticates with
`github-oidc` using the id-token permission it already holds, and republishes `server.json` — which step 2
regenerated with the new version. Nothing to run and nothing to log in to; just confirm it landed:

```bash
curl -sS "https://registry.modelcontextprotocol.io/v0/servers?search=agentic-chat&limit=3"
```

The entry should report `$1`, `status: active`, `isLatest: true`. If the workflow step failed instead, a 400
naming `ownership validation failed` means the published README lacks the `mcp-name:` marker — the marker has to
be inside the *released* artifact, not merely in git, so the fix is a later release rather than a retry.

**Context7**, which otherwise re-crawls a project of this size only every 45 days, so its answers would describe
the previous release for weeks:

```bash
curl -sS -X POST https://context7.com/api/v1/refresh \
  -H "Authorization: Bearer $CONTEXT7_API_KEY" -H "Content-Type: application/json" \
  -d '{"libraryName": "/amyodov/yet-another-agentic-chat"}'
```

The field is `libraryName` and the body must be JSON; a form-encoded post or a `libraryId` key returns a bare 500
that says nothing. Refreshes are rate-limited to one per 10 days, so
`{"error":"too-early", ...}` is a normal answer meaning the index is already recent enough — report it and move
on rather than retrying. Skip the step entirely if `CONTEXT7_API_KEY` is unset; the key comes from
context7.com/dashboard and is not required to release.

Both are written up for a human in `docs/development.md` under Publishing.

## 6. After

- If the install surface changed (entry points, commands), update the README's install section to match what
  the released version actually supports — the README must never advertise unreleased behavior. Re-read the
  `rules` in `context7.json` too: they are printed verbatim ahead of every answer Context7 gives about YAAC, so a
  stale one is wrong in public and invisible here.
- Check `docs/tools.md` is current (the `updating-mcp-tools` skill regenerates it) — the released docs should
  describe the released tools.
- Update the README's Status section if the release shipped or deferred features; it is the only feature record.
