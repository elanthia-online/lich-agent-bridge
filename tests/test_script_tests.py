"""Offline preparation tests use synthetic scripts; nothing reaches Lich."""

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from lich_agent_bridge.errors import ConfigurationError
from lich_agent_bridge.script_tests import prepare_script_suite, validate_script_suite


class ScriptSuitePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "scripts"
        self.root.mkdir()
        self.manifest = self.root / "suites" / "probe.json"
        self.manifest.parent.mkdir()
        for name in ("lab-test-runner.lic", "lab-test-runner.rb", "harmless-probe.lic"):
            (self.root / name).write_text("# Synthetic reviewed script\n")
        self.suite = {
            "version": 1, "id": "probe", "script": "harmless-probe",
            "files": ["harmless-probe.lic"],
            "cases": [{"id": "normal", "args": ["normal"], "assertions": [
                {"field": "room_id", "op": "unchanged"},
                {"field": "dead", "op": "equals", "value": False}]}],
            "limits": {"case_seconds": 2, "run_seconds": 10, "cleanup_seconds": 2},
        }
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.suite))

    def prepare(self):
        return prepare_script_suite(self.manifest, self.root, "Testchar", "1000")

    def test_prepares_exact_disabled_registration_without_writing_files(self):
        before = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        entry = self.prepare()
        after = {str(path): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(entry["name"], "test-probe")
        self.assertEqual(entry["script"], "lab-test-runner")
        self.assertEqual(entry["characters"], ["Testchar"])
        self.assertEqual(entry["safe_handoff"], {"kind": "room", "room_id": "1000"})
        self.assertEqual(entry["owner_scripts"], ["lab-test-runner", "harmless-probe"])
        self.assertEqual(entry["lanes"], ["movement", "combat"])
        self.assertEqual(entry["result_global"], "$lab_test_result")
        self.assertEqual(entry["signal_global"], "$lab_test_cancel")
        self.assertEqual(entry["capability_action"], "start")
        action = entry["actions"][0]
        self.assertEqual(action["command_template"], "lab-test probe {revision} {case_id}")
        self.assertEqual(action["script_args_template"], "probe {revision} {case_id}")
        self.assertEqual(action["launch_mode"], "start")
        self.assertTrue(action["policy"]["confirmation_required"])
        digest = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        self.assertEqual(action["parameters"][0]["values"], [digest])
        self.assertEqual(action["parameters"][1]["values"], ["normal", "all"])
        self.assertEqual(entry["test_suite"]["manifest"], "suites/probe.json")
        self.assertEqual(set(entry["test_suite"]["files"]), {
            "suites/probe.json", "lab-test-runner.lic", "lab-test-runner.rb", "harmless-probe.lic"})
        for name, actual in entry["test_suite"]["files"].items():
            self.assertEqual(actual, hashlib.sha256((self.root / name).read_bytes()).hexdigest())

    def test_changed_dependency_changes_pin_but_does_not_rewrite_manifest(self):
        before = self.prepare()
        (self.root / "harmless-probe.lic").write_text("# Revised synthetic fixture\n")
        after = self.prepare()
        self.assertNotEqual(before["test_suite"]["files"]["harmless-probe.lic"], after["test_suite"]["files"]["harmless-probe.lic"])
        self.assertEqual(before["actions"], after["actions"])

    def test_rejects_schema_extensions_missing_assertions_and_expression_operators(self):
        mutations = [
            lambda item: item.update(version=True),
            lambda item: item.update(unknown="authority"),
            lambda item: item.update(script="../other"),
            lambda item: item.update(script="lab-test-runner"),
            lambda item: item["cases"][0].update(id="all"),
            lambda item: item["cases"][0].update(assertions=[]),
            lambda item: item["cases"][0]["assertions"][0].update(field="eval"),
            lambda item: item["cases"][0]["assertions"][0].update(field=[]),
            lambda item: item["cases"][0]["assertions"][0].update(op="ruby"),
            lambda item: item["cases"][0]["assertions"][0].update(op=[]),
            lambda item: item["cases"][0]["assertions"][0].update(value=None),
            lambda item: item["cases"][0]["assertions"][1].update(value=None),
            lambda item: item["cases"][0]["assertions"][1].update(value=[]),
            lambda item: item["cases"][0].update(setup="arbitrary script"),
        ]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(self.suite)
                mutation(candidate)
                with self.assertRaises(ConfigurationError):
                    validate_script_suite(candidate)

    def test_rejects_unbounded_cases_args_and_time_limits(self):
        changes = [
            ("cases", []),
            ("cases", self.suite["cases"] * 2),
            ("cases", [{**self.suite["cases"][0], "id": f"case{i}"} for i in range(21)]),
            ("limits", {"case_seconds": 5, "run_seconds": 4, "cleanup_seconds": 1}),
            ("limits", {"case_seconds": 1, "run_seconds": 21, "cleanup_seconds": 1}),
            ("limits", {"case_seconds": 1, "run_seconds": 20, "cleanup_seconds": 4}),
            ("limits", {"case_seconds": True, "run_seconds": 20, "cleanup_seconds": 1}),
            ("limits", {"case_seconds": float("nan"), "run_seconds": 20, "cleanup_seconds": 1}),
            ("limits", {"case_seconds": 1, "run_seconds": 10 ** 500, "cleanup_seconds": 1}),
        ]
        for field, value in changes:
            candidate = copy.deepcopy(self.suite)
            candidate[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ConfigurationError):
                validate_script_suite(candidate)
        for args in (["ok"] * 33, ["two words"], ["a;b"], ["$(shell)"], ["/path"], ["a\nb"], ["x" * 129], [1]):
            candidate = copy.deepcopy(self.suite)
            candidate["cases"][0]["args"] = args
            with self.subTest(args=args), self.assertRaises(ConfigurationError):
                validate_script_suite(candidate)

    def test_rejects_traversal_missing_and_symlink_dependencies(self):
        outside = self.root.parent / "outside.rb"
        outside.write_text("# Outside private root\n")
        (self.root / "linked.rb").symlink_to(outside)
        for path in ("../outside.rb", "/outside.rb", "sub/../outside.rb", ".hidden.rb", "missing.rb", "linked.rb", "a\\b.rb"):
            self.suite["files"] = ["harmless-probe.lic", path]
            self.save()
            with self.subTest(path=path), self.assertRaises(ConfigurationError):
                self.prepare()

    def test_rejects_external_manifest_and_oversized_or_duplicate_key_json(self):
        outside = self.root.parent / "suite.json"
        outside.write_text(json.dumps(self.suite))
        with self.assertRaises(ConfigurationError):
            prepare_script_suite(outside, self.root, "Testchar", "1000")
        self.manifest.write_text(" " * 65537)
        with self.assertRaises(ConfigurationError):
            self.prepare()
        self.manifest.write_text('{"version":1,"version":1}')
        with self.assertRaises(ConfigurationError):
            self.prepare()

    def test_rejects_custom_shadowing_or_prefix_only_target(self):
        custom = self.root / "custom"
        custom.mkdir()
        shadow = custom / "harmless-probe.lic"
        shadow.write_text("# shadow\n")
        with self.assertRaises(ConfigurationError):
            self.prepare()
        shadow.unlink()
        (self.root / "harmless-probe.lic").rename(self.root / "harmless-probe-extra.lic")
        with self.assertRaises(ConfigurationError):
            self.prepare()

    def test_approved_custom_target_is_pinned_and_runner_helper_is_not_a_shadow(self):
        custom = self.root / "custom" / "reviewed"
        custom.mkdir(parents=True)
        (self.root / "harmless-probe.lic").rename(custom / "harmless-probe.lic")
        self.suite["files"] = ["custom/reviewed/harmless-probe.lic"]
        self.save()
        entry = self.prepare()
        self.assertIn("custom/reviewed/harmless-probe.lic", entry["test_suite"]["files"])

    def test_requires_explicit_single_character_and_numeric_room(self):
        for character, room in (("", "1000"), ("Test Other", "1000"), ("Testchar", ""), ("Testchar", "any"), ("Testchar", True)):
            with self.subTest(character=character, room=room), self.assertRaises(ConfigurationError):
                prepare_script_suite(self.manifest, self.root, character, room)

    def test_prepared_entry_is_accepted_by_shared_controller_parser(self):
        from lich_agent_bridge.controller_manifest import ControllerManifest

        private_registry = self.root.parent / "controllers.json"
        private_registry.write_text(json.dumps({"version": 1, "controllers": [self.prepare()]}))
        controller = ControllerManifest.load(private_registry).controller("test-probe")
        self.assertEqual(controller.test_suite["manifest"], "suites/probe.json")

    def test_sample_manifest_and_probe_prepare_as_written(self):
        samples = Path(__file__).resolve().parents[1] / "examples" / "script-tests"
        source = samples / "lab-lifecycle-probe.lic"
        (self.root / source.name).write_bytes(source.read_bytes())
        self.manifest.write_bytes((samples / "lifecycle-probe.json").read_bytes())
        entry = self.prepare()
        self.assertEqual(entry["name"], "test-lifecycle-probe")
        self.assertEqual(entry["actions"][0]["parameters"][1]["values"], ["normal", "intentional-error", "all"])

    def test_rejects_target_filename_case_that_runtime_cannot_accept(self):
        for filename in ("Harmless-Probe.lic", "harmless-probe.LIC"):
            with self.subTest(filename=filename):
                original = self.root / "harmless-probe.lic"
                changed = self.root / filename
                original.rename(changed)
                self.suite["files"] = [filename]
                self.save()
                try:
                    with self.assertRaises(ConfigurationError):
                        self.prepare()
                finally:
                    changed.rename(original)

    def test_rejects_uppercase_manifest_suffix_that_registry_cannot_accept(self):
        uppercase = self.manifest.with_suffix(".JSON")
        self.manifest.rename(uppercase)
        with self.assertRaises(ConfigurationError):
            prepare_script_suite(uppercase, self.root, "Testchar", "1000")

    @unittest.skipUnless(os.environ.get("RUBY") or shutil.which("ruby"), "Ruby is needed for cross-language contract verification")
    def test_shipped_example_preparation_loads_in_python_and_ruby_parsers(self):
        from lich_agent_bridge.controller_manifest import ControllerManifest

        repository = Path(__file__).resolve().parents[1]
        samples = repository / "examples" / "script-tests"
        for name in ("lab-test-runner.lic", "lab-test-runner.rb"):
            (self.root / name).write_bytes((repository / "lich" / name).read_bytes())
        (self.root / "lab-lifecycle-probe.lic").write_bytes((samples / "lab-lifecycle-probe.lic").read_bytes())
        self.manifest.write_bytes((samples / "lifecycle-probe.json").read_bytes())
        entry = self.prepare()
        registry = self.root.parent / "controllers.json"
        registry.write_text(json.dumps({"version": 1, "controllers": [entry]}))
        parsed = ControllerManifest.load(registry).controller("test-lifecycle-probe")
        self.assertEqual(parsed.test_suite, entry["test_suite"])
        program = """
require ARGV.fetch(0)
registry = LabControllerRegistry::Registry.load(ARGV.fetch(1))
controller = registry.controllers.first
metadata = controller.test_suite
revision = metadata.fetch('files').fetch(metadata.fetch('manifest'))
suite = LabTestRunner::Suite.new(
  root: ARGV.fetch(2), metadata: metadata, suite_id: 'lifecycle-probe',
  revision: revision, case_id: 'all', resolve: ->(name) { "#{name}.lic" })
puts JSON.generate({'controller' => controller.name,
                    'cases' => suite.cases.map { |item| item.fetch('id') },
                    'revision' => suite.revision})
"""
        result = subprocess.run(
            [os.environ.get("RUBY") or shutil.which("ruby"), "-e", program,
             str(repository / "lich" / "lab-controller-registry.rb"), str(registry), str(self.root)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = json.loads(result.stdout)
        self.assertEqual(evidence["controller"], "test-lifecycle-probe")
        self.assertEqual(evidence["cases"], ["normal", "intentional-error"])
        self.assertEqual(evidence["revision"], entry["test_suite"]["files"]["suites/probe.json"])


if __name__ == "__main__":
    unittest.main()
