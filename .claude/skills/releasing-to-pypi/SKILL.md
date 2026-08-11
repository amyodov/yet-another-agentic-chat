---
name: releasing-to-pypi
description: Release YAAC to PyPI - preconditions, version bump, git tag, GitHub release, publish workflow, post-release verification. Invoked by the user only; a release never happens on the assistant's own initiative.
argument-hint: [version]
disable-model-invocation: true
---

# Release YAAC

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
uv run python ${CLAUDE_SKILL_DIR}/generate_plugin.py
```

Commit all of it together. The version in
the code and the version being tagged must be the same string — a wheel that reports a different version than
its tag is a support puzzle nobody needs.

## 3. Tag and release

```bash
git push origin main
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

## 5. After

- If the install surface changed (entry points, commands), update the README's install section to match what
  the released version actually supports — the README must never advertise unreleased behavior.
- Nudge Context7, which otherwise re-crawls a project of this size only every 45 days, so its answers would
  describe the previous release for weeks. This and the other post-release chores, including the MCP registry,
  are written up for a human in `docs/development.md` under Publishing:

  ```bash
  curl -fsS -X POST https://context7.com/api/v1/refresh \
    -H "Authorization: Bearer $CONTEXT7_API_KEY" \
    -d 'libraryId=/amyodov/yet-another-agentic-chat'
  ```

  Skip it if `CONTEXT7_API_KEY` is unset -- the key comes from context7.com/dashboard and is not required to
  release. If the install surface changed, re-read the `rules` in `context7.json` too: they are printed verbatim
  ahead of every answer Context7 gives about YAAC, so a stale one is wrong in public and invisible here.
- Check `docs/tools.md` is current (the `updating-mcp-tools` skill regenerates it) — the released docs should
  describe the released tools.
- Update the README's Status section if the release shipped or deferred features; it is the only feature record.
