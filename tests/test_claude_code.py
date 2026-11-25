"""Claude Code chat-tracking layout: attachment, guards and packaging. No real hosts."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_factory as fixtures
from sfc_lib.common import common_dir, load_config, write_json
from sfc_lib.session_runtime import request_attachment

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / 'hooks/guard.py'


def host_env():
    return {k: v for k, v in os.environ.items() if k not in ('SFC_WORKER', 'SFC_TEST_CONTEXT')}


def hook(arguments, event, cwd):
    result = subprocess.run([sys.executable, '-B', str(GUARD), *arguments], cwd=cwd, input=json.dumps(event),
                            capture_output=True, text=True, timeout=30, env=host_env())
    return json.loads(result.stdout) if result.stdout.strip() else None


class ClaudeAttachmentTests(unittest.TestCase):
    tearDown = fixtures.IntegrationTests.tearDown

    def setUp(self):
        fixtures.IntegrationTests.setUp(self)
        write_json(self.root / '.sfc/config.json', self.c)
        self.c = load_config(self.root)
        # Claude Code's prompt event: no turn_id.
        self.event = {'hook_event_name': 'UserPromptSubmit', 'session_id': 'claude-session', 'cwd': str(self.root),
                      'permission_mode': 'default', 'prompt': 'Fix the export bug',
                      'transcript_path': '/tmp/transcript.jsonl'}

    def test_prompts_without_turn_ids_track_with_a_stable_chat_prefix(self):
        first = request_attachment(self.event, environ={}, host='claude')
        second = request_attachment(self.event, environ={}, host='claude')
        self.assertEqual(first['state'], 'tracking')
        self.assertEqual(Path(first['root']), self.root.resolve())
        self.assertEqual(first['prefix'], second['prefix'])
        self.assertTrue(first['prefix'].startswith('chat-'))
        self.assertNotEqual(first['turn'], second['turn'])
        other = request_attachment({**self.event, 'session_id': 'another-chat'}, environ={}, host='claude')
        self.assertNotEqual(other['prefix'], first['prefix'])
        # The same event without a host flag keeps the Codex contract.
        self.assertEqual(request_attachment(self.event, environ={})['state'], 'ignored')

    def test_session_start_sources_and_non_root_events(self):
        start = {**self.event, 'hook_event_name': 'SessionStart'}
        for source in ('startup', 'resume'):
            self.assertEqual(request_attachment({**start, 'source': source}, environ={}, host='claude')['state'],
                             'tracking')
        for event, environ in [({**start, 'source': 'clear'}, {}), ({**start, 'source': 'compact'}, {}),
                               ({**self.event, 'agent_id': 'subagent'}, {}), (self.event, {'SFC_WORKER': '1'}),
                               (self.event, {'SFC_TEST_CONTEXT': '1'}),
                               ({**self.event, 'hook_event_name': 'PreToolUse'}, {}),
                               ({**self.event, 'session_id': ' '}, {})]:
            with self.subTest(event=event, environ=environ):
                self.assertEqual(request_attachment(event, environ=environ, host='claude')['state'], 'ignored')

    def test_claude_never_requests_native_session_attachment(self):
        auth = common_dir(self.root) / 'sfc-autostart.json'
        write_json(auth, {'version': 2, 'id': 'a' * 32, 'root': str(self.root), 'enabled': True, 'state': 'armed'})
        (self.root / '.sfc/config.json').unlink()
        self.assertEqual(request_attachment(self.event, environ={}, host='claude')['state'], 'ignored')
        self.assertFalse((common_dir(self.root) / 'sfc-session-requests').exists())

    def test_unconfigured_and_non_git_folders_are_silent(self):
        (self.root / '.sfc/config.json').unlink()
        self.assertEqual(request_attachment(self.event, environ={}, host='claude')['state'], 'ignored')
        with tempfile.TemporaryDirectory() as plain:
            event = {**self.event, 'cwd': plain}
            self.assertEqual(request_attachment(event, environ={}, host='claude')['state'], 'ignored')
            self.assertIsNone(hook(['--attachment', '--host=claude'], event, plain))

    def test_prompt_hook_output_names_claude_code_and_the_event(self):
        output = hook(['--attachment', '--host=claude'], self.event, self.root)['hookSpecificOutput']
        self.assertEqual(output['hookEventName'], 'UserPromptSubmit')
        context = output['additionalContext']
        self.assertIn('Work directly in Claude Code without starting a separate worker', context)
        self.assertIn('record the task before editing', context)
        self.assertIn('track update --pr-url', context)
        self.assertIn('--delivery pr-required', context)
        start = hook(['--attachment', '--host=claude'], {**self.event, 'hook_event_name': 'SessionStart',
                                                         'source': 'startup'}, self.root)
        self.assertEqual(start['hookSpecificOutput']['hookEventName'], 'SessionStart')
        codex = hook(['--attachment'], {**self.event, 'turn_id': 'turn-1'}, self.root)
        self.assertIn('Work directly in Codex without', codex['hookSpecificOutput']['additionalContext'])

    def test_pre_tool_use_denies_claude_edits_outside_the_checkout(self):
        event = {'hook_event_name': 'PreToolUse', 'session_id': 'claude-session', 'cwd': str(self.root),
                 'tool_name': 'Write', 'tool_input': {'file_path': str(Path.home() / '.sfc-outside.txt'), 'content': 'x'}}
        denied = hook(['--host=claude'], event, self.root)['hookSpecificOutput']
        self.assertEqual(denied['permissionDecision'], 'deny')
        self.assertIn('escapes', denied['permissionDecisionReason'])
        allowed = {**event, 'tool_input': {'file_path': str(self.root / 'notes.txt'), 'content': 'x'}}
        self.assertIsNone(hook(['--host=claude'], allowed, self.root))


class ClaudeGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('sfc_claude_guard', GUARD)
        cls.guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.guard)

    def check(self, tool, data):
        return self.guard.check({'tool_name': tool, 'cwd': '/tmp/project', 'tool_input': data},
                                Path('/tmp/project'), fixtures.BASE_CONFIG, chat=True)

    def test_claude_file_tools(self):
        secret = "key='sk-" + 'x' * 30 + "'"
        self.assertIsNone(self.check('Write', {'file_path': '/tmp/project/a.py', 'content': 'x = 1\n'}))
        self.assertIsNone(self.check('Edit', {'file_path': 'a.py', 'old_string': 'a', 'new_string': 'b'}))
        self.assertIsNotNone(self.check('Write', {'file_path': '/opt/sfc-elsewhere/a.py', 'content': ''}))
        self.assertIsNotNone(self.check('Edit', {'file_path': '/tmp/project/.env', 'old_string': 'a', 'new_string': 'b'}))
        self.assertIsNotNone(self.check('Write', {'file_path': 'a.py', 'content': secret}))
        self.assertIsNotNone(self.check('MultiEdit', {'file_path': 'a.py', 'edits': [{'old_string': 'a', 'new_string': secret}]}))
        self.assertIsNotNone(self.check('NotebookEdit', {'notebook_path': '/opt/sfc-elsewhere/n.ipynb', 'new_source': ''}))
        self.assertIsNotNone(self.check('Write', {'content': 'x'}))

    def test_temporary_folders_accept_scratch_writes_only(self):
        secret = "key='sk-" + 'x' * 30 + "'"
        with tempfile.TemporaryDirectory() as scratch:
            draft = str(Path(scratch) / 'nested' / 'draft.md')
            self.assertIsNone(self.check('Write', {'file_path': draft, 'content': 'notes\n'}))
            self.assertIsNone(self.check('Edit', {'file_path': draft, 'old_string': 'a', 'new_string': 'b'}))
            self.assertIsNone(self.check('Write', {'file_path': '/tmp/sfc-scratch.txt', 'content': 'x'}))
            self.assertIsNotNone(self.check('Write', {'file_path': str(Path(scratch) / '.env'), 'content': 'x'}))
            self.assertIsNotNone(self.check('Write', {'file_path': draft, 'content': secret}))
            # A link inside a temporary folder cannot reach the home folder.
            link = Path(scratch) / 'home-link'
            link.symlink_to(Path.home())
            self.assertIsNotNone(self.check('Write', {'file_path': str(link / '.sfc-probe'), 'content': 'x'}))
        self.assertIsNotNone(self.check('Write', {'file_path': str(Path.home() / '.sfc-probe'), 'content': 'x'}))

    def test_broad_tmpdir_values_grant_nothing(self):
        home = Path.home().resolve()
        for value in ('/', str(Path.home()), str(home.parent)):
            with self.subTest(value=value):
                roots = self.guard.scratch_roots({'TMPDIR': value})
                self.assertNotIn(Path('/'), roots)
                self.assertFalse(any(home.is_relative_to(root) for root in roots))
        self.assertIn(Path(tempfile.gettempdir()).resolve(), self.guard.scratch_roots({}))

    def test_bash_guards_are_shared(self):
        self.assertIsNotNone(self.check('Bash', {'command': 'git push --force origin feature'}))
        self.assertIsNotNone(self.check('Bash', {'command': 'cat .env.local'}))


class ClaudePackagingTests(unittest.TestCase):
    def test_layout_excludes_codex_files_and_carries_claude_hooks(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        from package_plugin import claude_contents
        from validate_plugin import validate
        files = claude_contents()
        for name in ('.claude-plugin/plugin.json', '.claude-plugin/marketplace.json', 'hooks/guard.py',
                     'scripts/sfc.py', 'scripts/sfc_lib/session_runtime.py', 'skills/sfc/SKILL.md',
                     'docs/CLAUDE-CODE.md', 'LICENSE'):
            self.assertIn(name, files)
        self.assertEqual(json.loads(files['hooks/hooks.json']),
                         json.loads((ROOT / 'claude/hooks/hooks.json').read_text()))
        manifest = json.loads(files['.claude-plugin/plugin.json'])
        self.assertEqual(manifest['version'], validate()['version'])
        for name in files:
            with self.subTest(name=name):
                self.assertNotIn(name, ('.mcp.json', 'plugin.json'))
                self.assertFalse(name.startswith(('.codex-plugin', '.agents', 'claude/', 'tests/')))
                self.assertNotIn('/agents/openai.yaml', name)


    def test_archive_and_checksum_keep_the_full_version_name(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        from package_plugin import build_claude
        with tempfile.TemporaryDirectory() as dist:
            result = build_claude(dist, '0.2.2', {'README.md': b'fixture\n'})
            names = sorted(p.name for p in Path(dist).iterdir())
            self.assertEqual(names, ['sfc-0.2.2-claude', 'sfc-0.2.2-claude.zip', 'sfc-0.2.2-claude.zip.sha256'])
            self.assertEqual(Path(result['archive']).name, 'sfc-0.2.2-claude.zip')
            self.assertTrue(Path(dist, 'sfc-0.2.2-claude.zip.sha256').read_text().endswith('  sfc-0.2.2-claude.zip\n'))
            self.assertEqual(Path(result['directory'], 'README.md').read_bytes(), b'fixture\n')
            with self.assertRaises(FileExistsError):
                build_claude(dist, '0.2.2', {'README.md': b'other\n'})


if __name__ == '__main__':
    unittest.main()
