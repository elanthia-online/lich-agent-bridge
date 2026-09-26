"""Optional local semantic configuration keeps the default install dependency-free."""

import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from lich_agent_bridge import labctl
from lich_agent_bridge.errors import ConfigurationError
from lich_agent_bridge.settings import Settings


class SemanticSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / 'configuration' / 'lab.toml'
        self.environment = {'HOME': str(self.root)}

    def settings(self, value=None):
        return Settings.load(self.config, environment=self.environment, overrides={
            'knowledge': {'semantic_model_directory': value}})

    def test_default_is_off_and_optional_path_round_trips(self):
        default = Settings.load(self.config, environment=self.environment)
        self.assertIsNone(default.knowledge.semantic_model_directory)
        self.assertIsNone(default.to_mapping()['knowledge']['semantic_model_directory'])
        configured = self.settings('models/local-minilm')
        expected = self.config.parent / 'models' / 'local-minilm'
        self.assertEqual(configured.knowledge.semantic_model_directory, expected)
        configured.write()
        self.assertIn('semantic_model_directory = ', self.config.read_text())
        restored = Settings.load(self.config, environment=self.environment)
        self.assertEqual(restored.knowledge.semantic_model_directory, expected)
        self.assertEqual(restored.redacted()['knowledge']['semantic_model_directory'], str(expected))

    def test_rejects_invalid_path_types_blank_paths_and_unknown_keys(self):
        for value in (False, 12, [], {}, '', '  ', 'bad\x00path', '~someone/model'):
            with self.subTest(value=value), self.assertRaises(ConfigurationError):
                self.settings(value)
        with self.assertRaises(ConfigurationError):
            Settings.load(self.config, environment=self.environment, overrides={
                'knowledge': {'semantic_model': 'somewhere'}})

    def test_home_relative_path_and_explicit_disable(self):
        self.assertEqual(self.settings('~/models').knowledge.semantic_model_directory, self.root / 'models')
        self.settings('~/models').write()
        disabled = self.settings(None)
        self.assertIsNone(disabled.knowledge.semantic_model_directory)
        disabled.write(replace=True)
        self.assertNotIn('semantic_model_directory', self.config.read_text())

    def test_guided_knowledge_setup_offers_one_optional_model_path(self):
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            if 'Sections to edit' in prompt:
                return 'knowledge'
            if prompt.startswith('Optional local semantic model directory'):
                return '~/models/local-minilm'
            return ''

        with patch.dict(os.environ, self.environment, clear=True), patch('builtins.input', side_effect=answer), \
             redirect_stdout(io.StringIO()):
            labctl.main(['--config', str(self.config), 'setup'])
        configured = Settings.load(self.config, environment=self.environment)
        self.assertEqual(configured.knowledge.semantic_model_directory, self.root / 'models' / 'local-minilm')
        self.assertEqual(sum('semantic model' in prompt for prompt in prompts), 1)

    def test_doctor_off_does_not_load_or_inspect_a_model(self):
        inspect = Mock(side_effect=AssertionError('disabled model must not be inspected'))
        with patch.dict(sys.modules, {'lich_agent_bridge.semantic': SimpleNamespace(inspect_model=inspect)}):
            result = labctl._semantic_check(self.settings())
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['semantic_status'], 'disabled')
        inspect.assert_not_called()

    def test_doctor_delegates_readiness_without_inference_or_revalidation(self):
        configured = self.settings('~/models')
        for status in ('missing_dependencies', 'invalid_model', 'ready'):
            inspect = Mock(return_value={'status': status, 'detail': 'synthetic readiness'})
            with self.subTest(status=status), patch.dict(sys.modules, {
                    'lich_agent_bridge.semantic': SimpleNamespace(inspect_model=inspect)}):
                result = labctl._semantic_check(configured)
            self.assertEqual(result['semantic_status'], status)
            self.assertEqual(result['status'], 'ok' if status == 'ready' else 'warning')
            inspect.assert_called_once_with(configured.knowledge.semantic_model_directory)

    def test_doctor_report_includes_the_optional_model_check(self):
        configured = self.settings('~/models')
        configured.write()
        inspect = Mock(return_value={'status': 'ready', 'detail': 'synthetic readiness'})
        with patch.dict(os.environ, self.environment, clear=True), patch.dict(sys.modules, {
                'lich_agent_bridge.semantic': SimpleNamespace(inspect_model=inspect)}), \
             patch.object(labctl, '_service_check', return_value={'name': 'service', 'status': 'ok'}), \
             patch.object(labctl, '_backend_check', return_value={'name': 'backend', 'status': 'ok'}):
            report = labctl._doctor(self.config)
        model = next(check for check in report['checks'] if check['name'] == 'semantic_model')
        self.assertEqual(model['semantic_status'], 'ready')
        inspect.assert_called_once_with(configured.knowledge.semantic_model_directory)


if __name__ == '__main__':
    unittest.main()
