#!/usr/bin/env python3
"""Validate the distributable package without installing it or calling services."""
import ast
import json
from pathlib import Path
import re
import sys
from sfc_lib import __version__

ROOT = Path(__file__).resolve().parents[1]


def validate():
    manifest = json.loads((ROOT / "plugin.json").read_text())
    compatibility = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
    assert manifest["name"] == compatibility["name"] == "sfc"
    assert manifest["version"] == compatibility["version"] == __version__
    assert manifest["extensions"]["com.openai"]["interface"]["displayName"] == "SFC"
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
            json.loads(file.read_text())
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text())
    assert manifest['extensions']['com.openai']['mcpServers'] == compatibility['mcpServers'] == './.mcp.json'
    servers = json.loads((ROOT / '.mcp.json').read_text())['mcpServers']
    assert set(servers) == {'sfc_session'}
    assert servers['sfc_session'] == {'command': 'python3', 'args': ['${PLUGIN_ROOT}/scripts/session_mcp.py'],
                                      'env_vars': ['SFC_WORKER', 'SFC_TEST_CONTEXT']}
    assert (ROOT / 'scripts/session_mcp.py').is_file()
    for groups in hooks["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                if hook['type'] == 'command':
                    assert '${PLUGIN_ROOT}/hooks/guard.py' in hook["command"]
                else:
                    assert hook['type'] == 'mcp_tool' and hook['server'] == 'sfc_session'
                    assert hook['tool'] in ('session_attach', 'session_status')
                    assert hook['timeout'] <= 5
    assert 'SessionEnd' not in hooks['hooks']
    assert (ROOT / "LICENSE").is_file()
    return {"valid": True, "name": manifest["name"], "version": manifest["version"], "skills": sorted(found)}


if __name__ == "__main__":
    try:
        print(json.dumps(validate(), indent=2))
    except (AssertionError, KeyError, ValueError, OSError) as exc:
        print(f"Plugin validation failed: {exc}", file=sys.stderr)
        sys.exit(1)
