"""Claude Code sessions in the maintainer checkout must not launch the Codex session MCP."""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from package_plugin import ROOT, claude_contents, contents
from validate_plugin import maintainer_workspace, validate_maintainer_workspace


class MaintainerWorkspaceTests(unittest.TestCase):
    def temporary_directory(self):
        # TestCase.enterContext is Python 3.11+; SFC supports 3.10.
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def workspace(self, settings, servers=None):
        root = self.temporary_directory()
        shutil.copy(ROOT / '.mcp.json', root / '.mcp.json')
        if servers is not None:
            (root / '.mcp.json').write_text(json.dumps({'mcpServers': servers}))
        if settings is not None:
            (root / '.claude').mkdir()
            (root / '.claude/settings.json').write_text(settings if isinstance(settings, str) else json.dumps(settings))
        return root

    def test_checked_in_settings_disable_the_codex_session_server(self):
        settings = json.loads((ROOT / '.claude/settings.json').read_text())
        self.assertIn('sfc_session', settings['disabledMcpjsonServers'])
        validate_maintainer_workspace(ROOT)

    def test_missing_or_incomplete_settings_are_rejected(self):
        for settings in (None, {}, {'disabledMcpjsonServers': []}, {'disabledMcpjsonServers': 'sfc_session'},
                         {'disabledMcpjsonServers': [1]}, 'not json'):
            with self.subTest(settings=settings), self.assertRaises((AssertionError, OSError, ValueError)):
                validate_maintainer_workspace(self.workspace(settings))

    def test_every_codex_macro_server_must_be_disabled(self):
        servers = {'sfc_session': {'command': 'python3', 'args': ['${PLUGIN_ROOT}/scripts/session_mcp.py']},
                   'other_codex': {'command': 'python3', 'args': ['${PLUGIN_ROOT}/x.py']},
                   'portable': {'command': 'node', 'args': ['server.js']}}
        with self.assertRaisesRegex(AssertionError, 'other_codex'):
            validate_maintainer_workspace(self.workspace({'disabledMcpjsonServers': ['sfc_session']}, servers))
        validate_maintainer_workspace(self.workspace({'disabledMcpjsonServers': ['sfc_session', 'other_codex']},
                                                     servers))

    def test_only_the_maintainer_source_tree_requires_the_setting(self):
        self.assertTrue(maintainer_workspace(ROOT))
        export = self.temporary_directory()
        for name, data in contents().items():
            (export / name).parent.mkdir(parents=True, exist_ok=True)
            (export / name).write_bytes(data)
        (export / '.git').mkdir()  # a git repository rebuilt from package contents
        self.assertFalse(maintainer_workspace(export))

    def test_claude_settings_are_never_packaged(self):
        for files in (contents(), contents(codex_root=Path(tempfile.gettempdir()).resolve() / 'sfc-native'),
                      claude_contents()):
            self.assertFalse([name for name in files if name.startswith('.claude/')])
        self.assertIn('.mcp.json', contents())
        self.assertNotIn('.mcp.json', claude_contents())


if __name__ == '__main__':
    unittest.main()
