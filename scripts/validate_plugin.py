#!/usr/bin/env python3
"""Validate the distributable package without installing it or calling services."""
import ast
import json
from pathlib import Path
import re
import sys
from sfc_lib import __version__

ROOT = Path(__file__).resolve().parents[1]


def validate_output_schema(schema, location):
    if isinstance(schema, list):
        for index, child in enumerate(schema):
            validate_output_schema(child, f'{location}[{index}]')
    elif isinstance(schema, dict):
        assert 'uniqueItems' not in schema, f'Codex strict output does not support uniqueItems in {location}'
        if 'properties' in schema:
            assert schema.get('additionalProperties') is False, f'Open object in {location}'
            assert set(schema.get('required', [])) == set(schema['properties']), (
                f'Every output property must be required in {location}; use null for an absent value')
        for key, child in schema.items():
            validate_output_schema(child, f'{location}.{key}')


def maintainer_workspace(root):
    """The maintainer source tree, not a package export or a checkout of one.

    Packages never contain AGENTS.md, so installed copies, release candidates
    and git repositories rebuilt from package contents are exempt.
    """
    return (root / 'AGENTS.md').is_file()


def validate_maintainer_workspace(root):
    """Claude Code sessions in this checkout must not start Codex MCP servers.

    Claude Code reads the root .mcp.json as project MCP configuration, but its
    launch arguments use Codex's ${PLUGIN_ROOT} macro, which Claude Code leaves
    empty. The checked-in .claude/settings.json disables every such server. It is
    maintainer configuration only and is never part of a package.
    """
    settings = json.loads((root / '.claude/settings.json').read_text())
    disabled = settings.get('disabledMcpjsonServers')
    assert isinstance(disabled, list) and all(isinstance(v, str) for v in disabled), \
        '.claude/settings.json disabledMcpjsonServers must be a list of server names'
    servers = json.loads((root / '.mcp.json').read_text())['mcpServers']
    codex_only = {name for name, server in servers.items() if '${PLUGIN_ROOT}' in json.dumps(server)}
    assert codex_only <= set(disabled), \
        f'Claude Code would launch Codex-only MCP servers: {sorted(codex_only - set(disabled))}'


def validate():
    compatibility = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    portable = (ROOT / 'plugin.json').exists()
    manifest = json.loads((ROOT / 'plugin.json').read_text()) if portable else compatibility
    assert manifest["name"] == compatibility["name"] == "sfc"
    assert manifest["version"] == compatibility["version"] == __version__
    overlay = manifest['extensions']['com.openai'] if portable else compatibility
    assert overlay['interface']['displayName'] == 'SFC'
    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text())
    assert marketplace["plugins"][0]["source"]["path"] == "./"
    assert marketplace["plugins"][0]["name"] == manifest["name"]
    expected = {"sfc", "sfc-build", "sfc-qa", "sfc-security", "sfc-review", "sfc-collect",
                "sfc-ui-refine", "sfc-visual", "sfc-git-sync"}
    found = set()
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        source = skill.read_text()
        front = re.match(r"\A---\n(.*?)\n---\n", source, re.S)
        assert front, f"Missing skill frontmatter: {skill}"
        name = re.search(r"^name: (.+)$", front[1], re.M)[1]
        assert name == skill.parent.name and re.fullmatch(r"[a-z0-9-]{1,64}", name)
        assert re.search(r"^description: .{25,}$", front[1], re.M)
        assert (skill.parent / "agents/openai.yaml").is_file()
        assert "TODO" not in source and "[insert" not in source.lower()
        found.add(name)
    assert found == expected, f"Unexpected skill set: {found}"
    for file in [*(ROOT / "scripts").rglob("*.py"), *(ROOT / "hooks").glob("*.py")]:
        ast.parse(file.read_text(), filename=str(file))
    for file in ROOT.rglob("*.json"):
        if not any(part in (".git", ".sfc", "dist") for part in file.relative_to(ROOT).parts):
            value = json.loads(file.read_text())
            if file.parent == ROOT / 'schemas':
                validate_output_schema(value, file.name)
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())
    assert overlay['mcpServers'] == compatibility['mcpServers'] == './.mcp.json'
    servers = json.loads((ROOT / '.mcp.json').read_text())['mcpServers']
    assert set(servers) == {'sfc_session'}
    launch = '${PLUGIN_ROOT}/scripts/session_mcp.py' if portable else str(ROOT / 'scripts/session_mcp.py')
    assert servers['sfc_session'] == {'command': 'python3', 'args': ['-B', launch],
                                      'env_vars': ['SFC_WORKER', 'SFC_TEST_CONTEXT']}
    assert (ROOT / 'scripts/session_mcp.py').is_file()
    for groups in hooks["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook['type'] == 'command':
                    assert '${PLUGIN_ROOT}/hooks/guard.py' in hook["command"]
                else:
                    assert hook['type'] == 'mcp_tool' and hook['server'] == 'sfc_session'
                    assert hook['tool'] in ('session_attach', 'session_attach_hook', 'session_status')
                    assert hook['timeout'] <= 5
    assert 'SessionEnd' not in hooks['hooks']
    if (ROOT / "claude").is_dir():
        # Source checkouts carry the Claude Code overlay; the Codex package does not.
        claude = json.loads((ROOT / "claude/.claude-plugin/plugin.json").read_text())
        assert claude["name"] == "sfc" and claude["version"] == __version__
        market = json.loads((ROOT / "claude/.claude-plugin/marketplace.json").read_text())
        assert [p["name"] for p in market["plugins"]] == ["sfc"] and market["plugins"][0]["source"] == "./"
        claude_hooks = json.loads((ROOT / "claude/hooks/hooks.json").read_text())["hooks"]
        assert set(claude_hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse"}
        for groups in claude_hooks.values():
            for group in groups:
                for hook in group["hooks"]:
                    assert hook["type"] == "command" and "--host=claude" in hook["command"]
                    assert '"${CLAUDE_PLUGIN_ROOT}/hooks/guard.py"' in hook["command"]
                    assert "${PLUGIN_ROOT}" not in hook["command"].replace("${CLAUDE_PLUGIN_ROOT}", "")
        assert not (ROOT / "claude/.mcp.json").exists(), "The Codex session MCP is not part of the Claude layout"
    if maintainer_workspace(ROOT):
        validate_maintainer_workspace(ROOT)
    assert (ROOT / "LICENSE").is_file()
    return {"valid": True, "name": manifest["name"], "version": manifest["version"],
            "format": 'portable-source' if portable else 'codex-resolved', "skills": sorted(found)}


if __name__ == "__main__":
    try:
        print(json.dumps(validate(), indent=2))
    except (AssertionError, KeyError, ValueError, OSError) as exc:
        print(f"Plugin validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
