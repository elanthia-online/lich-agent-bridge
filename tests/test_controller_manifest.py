import json
import tempfile
import unittest
from pathlib import Path

from lich_agent_bridge.controller_manifest import ControllerManifest
from lich_agent_bridge.errors import ConfigurationError, ValidationError


class ControllerManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = ControllerManifest.load(Path(__file__).parent / "fixtures" / "controllers.json")

    def test_shipped_manifest_is_valid_and_has_no_bundled_controllers(self):
        manifest = ControllerManifest.load(Path(__file__).parents[1] / "lich" / "lab-controllers.json")
        self.assertEqual(manifest.controllers, ())
        self.assertIsNone(manifest.match_command("lab-test-hunt start test-hunt"))

    def test_eohunter_example_is_opt_in_bounded_and_exact(self):
        manifest = ControllerManifest.load(
            Path(__file__).parents[1] / "examples" / "controllers" / "eohunter-trial-campaign.json"
        )
        controller = manifest.controller("eohunter-trial")
        self.assertEqual(controller.safe_handoff["kind"], "controller_refuge")
        self.assertEqual(controller.safe_room({}), "1000")
        command, script_args, arguments = controller.action("start").build(
            {"profile": "Reviewed-Trial", "trial": "a-b-c"}
        )
        self.assertEqual(command, "eohunter Reviewed-Trial trial a-b-c")
        self.assertEqual(script_args, "Reviewed-Trial trial a-b-c")
        self.assertEqual(arguments, {"profile": "Reviewed-Trial", "trial": "a-b-c"})
        with self.assertRaises(ValidationError):
            controller.action("start").build(
                {"profile": "Reviewed-Trial", "trial": "a-b-d"}
            )

    def test_manifest_exposes_all_controllers_and_testknight_hunt(self):
        self.assertEqual(
            [item.name for item in self.manifest.controllers],
            ["engage", "room", "hunt", "rift"],
        )
        hunt = self.manifest.controller("hunt")
        self.assertTrue(hunt.available_for("testknight"))
        self.assertEqual(hunt.lanes, frozenset({"movement", "combat"}))
        self.assertEqual(
            hunt.safe_room({"profile": "test-hunt"}), "1000"
        )

    def test_commands_are_exact_anchored_and_build_from_strict_arguments(self):
        matched = self.manifest.match_command(
            "LAB-TEST-ENGAGE TEST-LIVING #185539534 LOOT"
        )
        self.assertEqual(matched.controller.name, "engage")
        self.assertEqual(matched.arguments["target_id"], "185539534")
        self.assertTrue(matched.arguments["loot"])
        self.assertEqual(
            matched.script_args, "test-living #185539534 loot"
        )
        self.assertIsNone(
            self.manifest.match_command("lab-test-hunt start test-hunt; north")
        )
        action = self.manifest.controller("hunt").action("start")
        command, script_args, arguments = action.build(
            {"profile": "test-probe"}
        )
        self.assertEqual(command, "lab-test-hunt start test-probe")
        self.assertEqual(script_args, "test-probe")
        self.assertEqual(arguments, {"profile": "test-probe"})
        with self.assertRaises(ValidationError):
            action.build({"profile": "test-hunt", "extra": True})

    def test_invalid_manifest_fails_closed(self):
        source = json.loads(self.manifest.path.read_text(encoding="utf-8"))
        source["controllers"][0]["surprise"] = True
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controllers.json"
            path.write_text(json.dumps(source), encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unsupported field"):
                ControllerManifest.load(path)


if __name__ == "__main__":
    unittest.main()
