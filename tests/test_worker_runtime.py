import unittest

from scripts.sfc_lib import worker_runtime
from scripts.sfc_lib.common import Halt


class WorkerRuntimeTests(unittest.TestCase):
    def test_legacy_and_explicit_inheritance_keep_codex_settings(self):
        for config in ({}, {'worker_runtime': {}},
                       {'worker_runtime': {'skill_catalog': True, 'memories': True}}):
            with self.subTest(config=config):
                worker_runtime.validate(config)
                self.assertEqual(worker_runtime.arguments(config), [])

    def test_controls_only_add_supported_context_overrides(self):
        config = {'model': 'gpt-6.1-sol', 'reasoning_effort': 'high', 'service_tier': 'fast',
                  'worker_runtime': {'skill_catalog': False, 'memories': False}}
        expected = ['-c', 'skills.include_instructions=false',
                    '-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false']
        self.assertEqual(worker_runtime.arguments(config), expected)
        self.assertEqual(worker_runtime.receipt(config), {
            'version': 1,
            'settings': {'skill_catalog': False, 'memories': False},
            'arguments': expected,
        })

    def test_controls_can_be_selected_independently(self):
        self.assertEqual(worker_runtime.arguments({'worker_runtime': {'skill_catalog': False}}),
                         ['-c', 'skills.include_instructions=false'])
        self.assertEqual(worker_runtime.arguments({'worker_runtime': {'memories': False}}),
                         ['-c', 'memories.use_memories=false', '-c', 'memories.generate_memories=false'])

    def test_invalid_settings_are_rejected(self):
        for value in (None, [], 'ambient', {'apps': False}, {'skill_catalog': 0}, {'memories': 'false'}):
            with self.subTest(value=value), self.assertRaisesRegex(Halt, 'worker_runtime'):
                worker_runtime.validate({'worker_runtime': value})
