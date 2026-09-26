"""Synthetic script-test CLI transport fences; no runtime or game connection."""

import io
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

from lich_agent_bridge import labctl


class ScriptTestCliTests(unittest.TestCase):
    def test_prepare_is_offline_and_emits_only_registration(self):
        output = io.StringIO()
        with patch("lich_agent_bridge.script_tests.prepare_script_suite", return_value={"name": "test-probe"}) as prepare, \
                patch.object(labctl.Settings, "load") as settings, \
                patch.object(labctl, "_post") as post, redirect_stdout(output):
            labctl.main(["tests", "prepare", "/tmp/test-suite.json",
                         "--scripts-dir", "/tmp/test-scripts", "--character", "Testmage",
                         "--room-id", "123"])
        prepare.assert_called_once_with(Path("/tmp/test-suite.json"), Path("/tmp/test-scripts"), "Testmage", "123")
        settings.assert_not_called()
        post.assert_not_called()
        self.assertIn('"test-probe"', output.getvalue())

    def test_perform_preserves_expected_generation(self):
        with patch.object(labctl, "_token", return_value="test-token"), \
                patch.object(labctl, "_post", return_value={"operation_id": "op-test"}) as post, \
                redirect_stdout(io.StringIO()):
            labctl.main(["perform", "Testmage", "controller.test-probe",
                         "--expected-generation", "generation-test",
                         "--arg", 'case_id="all"'])
        self.assertEqual(post.call_args.args[1]["expected_generation"], "generation-test")
        self.assertEqual(post.call_args.args[1]["args"], {"case_id": "all"})

    def test_exact_stop_preserves_identity(self):
        with patch.object(labctl, "_token", return_value="test-token"), \
                patch.object(labctl, "_post", return_value={"stopped": False}) as post, \
                redirect_stdout(io.StringIO()):
            labctl.main(["stop", "Testmage", "--operation-id", "op-test",
                         "--expected-generation", "generation-test"])
        self.assertEqual(post.call_args.args[1], {
            "character": "Testmage", "operation_id": "op-test",
            "expected_generation": "generation-test"})

    def test_outing_budget_is_forwarded_separately_from_watch_timeout(self):
        with patch.object(labctl, "_token", return_value="test-token"), \
                patch.object(labctl, "_post", return_value={"operation_id": "op-test"}) as post, \
                redirect_stdout(io.StringIO()):
            labctl.main(["perform", "Testmage", "controller.quick", "--expected-generation", "generation-test",
                         "--operation-timeout", "90", "--timeout", "0"])
        self.assertEqual(post.call_args.args[1]["timeout_seconds"], 90)

    def test_invalid_outing_budget_does_not_reach_transport(self):
        with patch.object(labctl, "_token", return_value="test-token"), patch.object(labctl, "_post") as post:
            for value in ("0", "-1", "301", "inf", "nan"):
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    labctl.main(["perform", "Testmage", "controller.quick", "--operation-timeout", value])
        post.assert_not_called()

    def test_partial_stop_identity_fails_before_transport(self):
        with patch.object(labctl, "_post") as post, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                labctl.main(["stop", "Testmage", "--operation-id", "op-test"])
        post.assert_not_called()
