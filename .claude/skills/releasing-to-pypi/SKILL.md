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

Set `version = "$1"` in `pyproject.toml`, run `uv sync` so `uv.lock` follows, and commit both. The version in
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
- Check `docs/tools.md` is current (the `updating-mcp-tools` skill regenerates it) — the released docs should
  describe the released tools.
- Update the README's Status section if the release shipped or deferred features; it is the only feature record.
