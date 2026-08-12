---
name: updating-mcp-tools
description: The workflow for writing or changing YAAC's MCP tool definitions in src/yaac/frontend.py. Use whenever a tool is added, removed or renamed, a docstring, parameter or Field description is edited, or the server instructions change — even for a small wording tweak: first get the descriptions right, then regenerate docs/tools.md so the documented toolset matches what a client sees. Also use when docs/tools.md is suspected stale or its accuracy is questioned.
allowed-tools: Bash(uv run python ${CLAUDE_SKILL_DIR}/scripts/generate.py)
---

# Updating the MCP tool surface

Two steps, in order: get the descriptions right, then sync the generated docs. The order matters because the
docs are generated from the descriptions — polishing prose after regenerating means regenerating twice.

## 1. Description quality

The economics decide where words go: a tool description rides in **every request** while the server is
connected, and a tool result is paid **once per call**. So:

- Permanent duties go in descriptions, tersely; situational nudges go in results. A sentence in a description
  is charged to every turn of every session that has the server installed, joined or not.
- Descriptions must carry the whole usage flow **unaided** — no client is obliged to surface server
  instructions, and no skill is installed on the far side. The two flow-critical facts they must keep alive:
  joining commits the model to polling `check_inbox` every turn, and a reply only ever arrives through
  `check_inbox`. Weakening either turns a working radio into an apparently deaf one.
- Dormant-visible tools (`list_channels`, `join_channel`) are the whole context cost YAAC imposes on sessions
  that never join; be strictest about their length.
- Parameter `Field` descriptions are part of the surface too — a model fills arguments from them.

## 2. Sync the docs

`docs/tools.md` is generated, never hand-written. The generator imports `yaac.frontend` and reads the tool
registry itself — the same registration path that answers `tools/list` — so the file shows exactly what a
connected client sees: names, availability (dormant vs on-air), descriptions, parameter schemas, and the server
instructions. Editing it by hand would only create a version of the truth that the next regeneration deletes.

1. From the repository root, run:

   ```bash
   uv run python ${CLAUDE_SKILL_DIR}/scripts/generate.py
   ```

2. `git diff docs/tools.md` and read the diff. It should contain precisely the change made in step 1 — an
   unexpected hunk means either the docs were stale (fine, that is the point) or the change had a wider blast
   radius than intended (worth telling the user about).

3. Commit `docs/tools.md` in the same commit as the `frontend.py` change that caused it, so the two cannot
   drift apart in history.

If the generator fails, fix the cause rather than writing the docs manually — a failing import or a schema it
cannot render means `frontend.py` has a problem a client would also see.
