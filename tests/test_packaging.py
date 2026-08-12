"""The plugin manifests, which are generated and therefore able to go stale.

YAAC restates the same facts for three kinds of reader -- Agent Plugins 1.0.0 clients, Claude Code, and the
official MCP registry -- in manifests a generator writes. These check that what is on disk is what the generator
would write today, and that the two plugin standards agree about what they are installing.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from yaac import frontend

REPO = Path(__file__).resolve().parent.parent
GENERATOR = REPO / ".claude" / "skills" / "releasing" / "scripts" / "generate_manifests.py"


def generator():
    """Load the generator as a module, so the expectation comes from it rather than a second copy of the truth."""
    spec = importlib.util.spec_from_file_location("generate_manifests", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generated():
    gen = generator()
    return gen, {path: gen.render(content) for path, content in gen.files().items()}


def test_the_generator_writes_every_manifest_the_project_publishes(generated) -> None:
    """Agent Plugins wants plugin.json and mcp.json; Claude Code wants the same facts under .claude-plugin/ and
    .mcp.json, plus hooks/hooks.json, which Agent Plugins has no equivalent of; the marketplace at the repository
    root is what `/plugin marketplace add owner/repo` reads; and server.json is the MCP registry submission.
    Naming the set here is what makes a forgotten one visible."""
    _, rendered = generated
    assert set(rendered) == {
        "plugin/plugin.json",
        "plugin/mcp.json",
        "plugin/.claude-plugin/plugin.json",
        "plugin/.mcp.json",
        "plugin/hooks/hooks.json",
        ".claude-plugin/marketplace.json",
        "server.json",
    }


@pytest.mark.parametrize(
    "path",
    [
        "plugin/plugin.json",
        "plugin/mcp.json",
        "plugin/.claude-plugin/plugin.json",
        "plugin/.mcp.json",
        "plugin/hooks/hooks.json",
        ".claude-plugin/marketplace.json",
        "server.json",
    ],
)
def test_the_manifests_on_disk_are_current(generated, path: str) -> None:
    """pyproject.toml is the only place a version is written; every manifest restates it. Nobody keeps that many
    copies in step by hand, so a forgotten regeneration fails here rather than shipping a plugin that claims the
    wrong version."""
    _, rendered = generated
    assert (REPO / path).read_text(encoding="utf-8") == rendered[path]


def test_both_standards_install_the_same_server(generated) -> None:
    """The two MCP configs are separate files because only one of the standards promises to tolerate the other's
    shape. They must still describe the same thing, or a user gets a different YAAC depending on their client."""
    _, rendered = generated
    agent_plugins = json.loads(rendered["plugin/mcp.json"])
    claude_code = json.loads(rendered["plugin/.mcp.json"])
    assert agent_plugins == claude_code
    assert agent_plugins["mcpServers"]["yaac"] == {
        "type": "stdio",
        "command": "uvx",
        "args": ["yet-another-agentic-chat"],
    }


def test_the_version_matches_the_package(generated) -> None:
    """The plugin installs the published package, so claiming a version the package does not have is a lie the
    user can check against PyPI."""
    gen, rendered = generated
    version = gen.project()["version"]
    for path in ("plugin/plugin.json", "plugin/.claude-plugin/plugin.json"):
        assert json.loads(rendered[path])["version"] == version
    assert json.loads(rendered[".claude-plugin/marketplace.json"])["plugins"][0]["version"] == version
    server = json.loads(rendered["server.json"])
    assert server["version"] == version
    assert server["packages"][0]["version"] == version


def test_the_hooks_call_a_tool_this_server_really_has(generated) -> None:
    """The hook names its tool by string, on a server named by string, and neither is checked until it fires -- at
    which point a wrong name is a failed hook on every tool call. Both are pinned here instead.

    The server name is the plugin-scoped form Claude Code requires for a plugin-bundled server, which is not the
    bare key under `mcpServers`; getting that wrong is the easy mistake, and it fails the same silent way."""
    _, rendered = generated
    hooks = json.loads(rendered["plugin/hooks/hooks.json"])["hooks"]
    server = json.loads(rendered["plugin/.mcp.json"])["mcpServers"]
    entries = [entry for event in hooks.values() for group in event for entry in group["hooks"]]

    assert {entry["type"] for entry in entries} == {"mcp_tool"}
    assert {entry["server"] for entry in entries} == {"plugin:yaac:yaac"}
    assert {entry["tool"] for entry in entries} == {frontend.HOOK_TOOL}
    assert "yaac" in server  # the bare key the scoped name is built from
    assert frontend.HOOK_TOOL in {tool.__name__ for tool in (frontend.hook_report,)}
    # Every event it fires on, and the input each is given. PreToolUse alone can say what is about to run, which
    # is how delivery stands aside for check_inbox itself.
    assert {event: [entry["input"] for entry in group[0]["hooks"]] for event, group in hooks.items()} == {
        "PreToolUse": [{"event": "PreToolUse", "tool_name": "${tool_name}"}],
        "UserPromptSubmit": [{"event": "UserPromptSubmit"}],
        "Stop": [{"event": "Stop"}],
    }


def test_the_bundled_skill_is_discoverable(generated) -> None:
    """Both standards find skills at skills/<name>/SKILL.md, so one directory serves both. A skill without
    frontmatter name and description is one no client will offer."""
    skill = (REPO / "plugin" / "skills" / "yaac" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\n")
    frontmatter = skill.split("---", 2)[1]
    assert "name: yaac" in frontmatter
    assert "description:" in frontmatter


def test_the_registry_entry_fits_what_the_registry_accepts(generated) -> None:
    """server.schema.json caps description at 100 characters and constrains the name, and the namespace is not
    ours to choose: GitHub authentication only grants io.github.<user>/*, so anything else is rejected at publish
    time rather than at review time."""
    _, rendered = generated
    server = json.loads(rendered["server.json"])
    assert len(server["description"]) <= 100
    assert len(server["title"]) <= 100
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", server["name"])
    assert server["name"].startswith("io.github.amyodov/")
    assert server["packages"][0]["registryType"] == "pypi"
    assert server["packages"][0]["identifier"] == "yet-another-agentic-chat"
    assert server["packages"][0]["transport"] == {"type": "stdio"}


def test_the_readme_carries_the_ownership_marker(generated) -> None:
    """The registry proves ownership of a PyPI package by finding this in the published package description. It
    has to match the server name exactly, and it has to be in a release, not merely in git."""
    _, rendered = generated
    name = json.loads(rendered["server.json"])["name"]
    assert f"mcp-name: {name} -->" in (REPO / "README.md").read_text(encoding="utf-8")
