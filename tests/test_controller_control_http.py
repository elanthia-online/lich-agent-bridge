"""Real authenticated HTTP/CLI path with synthetic state and no game connection."""

import io
import json
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from lich_agent_bridge import labctl
from lich_agent_bridge.actions import ActionBroker, ActionControl, CommandPolicy
from lich_agent_bridge.controller_manifest import ControllerManifest
from lich_agent_bridge.operations import CapabilityRunner
from lich_agent_bridge.server import ServerConfig, build_server
from lich_agent_bridge.session_hub import SessionHub
from lich_agent_bridge.world_state import WorldState

from .fakes import RecordingModel
from .test_controller_controls import FIXTURE, WaitingEvidence
from .test_operations import BrokerDriver, FakeClock, FakeState
from .test_server import StaticInventory, StaticKnowledge


class ControllerControlHTTPTests(unittest.TestCase):
    TOKEN = "c" * 64
    ROUTE = "/v1/session/operation/control"

    def setUp(self):
        self.clock, self.state, self.evidence = FakeClock(), FakeState(), WaitingEvidence()
        self.state.fresh, self.state.sequence = True, 1
        self.state.dead, self.state.stunned = False, False
        self.state.room_id, self.state.scripts = "1000", ()
        self.state.owners = {"movement": None, "combat": None}
        manifest = ControllerManifest.load(FIXTURE)
        self.audit = []
        self.broker = ActionBroker(policy=CommandPolicy(manifest), clock=self.clock, audit=self.audit.append)
        self.broker.admit_generation("Testmage", "generation-1")
        self.broker.control(ActionControl(character="Testmage", enabled=True))
        driver = BrokerDriver(self.broker, self.state)

        def launched(_command):
            self.state.scripts = ("lab-test-quick", "bigshot")
            self.state.owners = {"movement": "lab-test-quick", "combat": "bigshot"}
            self.state.sequence += 1

        driver.after_command = launched
        self.runner = CapabilityRunner(
            actions=self.broker, state=self.state, evidence=self.evidence,
            controller_manifest=manifest, clock=self.clock, sleeper=self.clock.sleep, step_hook=driver,
        )
        self.model = RecordingModel()
        world, inventory, knowledge = WorldState(), StaticInventory(), StaticKnowledge()
        hub = SessionHub(world_state=world, actions=self.broker, inventory=inventory,
                         knowledge=knowledge, capabilities=self.runner)
        self.server = build_server(
            ServerConfig(port=0), model=self.model, actions=self.broker, action_token=self.TOKEN,
            world_state=world, inventory=inventory, knowledge=knowledge, session_hub=hub,
            controller_manifest=manifest, timing=lambda _sample: None,
        )
        self.worker = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True)
        self.worker.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        self.operation = self.runner.start("Testmage", "controller.quick", expected_generation="generation-1")
        self.assertTrue(self.evidence.entered.wait(0.5))
        self.payload = {"character": "Testmage", "operation_id": self.operation.operation_id,
                        "expected_generation": "generation-1", "control": "hold"}

    def tearDown(self):
        self.evidence.release.set()
        self.runner.wait(self.operation.operation_id, timeout_seconds=0.5)
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=1)

    def post(self, payload=None, *, token=TOKEN, route=ROUTE):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = Request(self.base_url + route, method="POST", headers=headers,
                          data=json.dumps(self.payload if payload is None else payload).encode())
        try:
            with urlopen(request, timeout=1) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            with error:
                return error.code, json.load(error)

    def test_real_route_delegates_to_broker_without_claiming_application(self):
        status, result = self.post()
        self.assertEqual(status, 202)
        self.assertEqual(result["operation_id"], self.operation.operation_id)
        self.assertIsNone(result["applied"])
        action = self.broker.get(result["action"]["action_id"])
        self.assertEqual(action["status"], "confirmation_required")
        self.assertEqual(action["command"], f"lab-test-quick hold {result['run_id']}")
        self.assertTrue(any(event.get("action_id") == action["action_id"] for event in self.audit))
        self.assertEqual(len(self.runner.history("Testmage")), 1)

    def test_combat_report_route_is_authenticated_read_only_and_character_bound(self):
        payload = {"character": "Testmage", "operation_id": self.operation.operation_id}
        route = "/v1/session/combat/report"
        self.assertEqual(self.post(payload, route=route, token=None)[0], 401)
        status, result = self.post(payload, route=route)
        self.assertEqual(status, 200)
        self.assertEqual(result["reason"], "operation_not_terminal")
        self.assertEqual(self.post({**payload, "character": "Other"}, route=route)[0], 400)
        self.assertEqual(len(self.runner.history("Testmage")), 1)
        self.assertEqual(self.model.calls, [])

    def test_missing_and_wrong_auth_are_rejected_before_delegation(self):
        before = len(self.audit)
        for token in (None, "wrong-synthetic-token"):
            with self.subTest(token=token):
                status, result = self.post(token=token)
                self.assertEqual(status, 401)
                self.assertEqual(result["error"], "unauthorized")
        self.assertEqual(len(self.audit), before)

    def test_unknown_operation_wrong_character_and_stale_generation_are_rejected(self):
        before = len(self.audit)
        for changed in ({"operation_id": "unknown-operation"}, {"character": "Othermage"},
                        {"expected_generation": "old-generation"}):
            with self.subTest(changed=changed):
                status, result = self.post({**self.payload, **changed})
                self.assertEqual(status, 400)
                self.assertEqual(result["error"], "invalid_request")
        self.assertEqual(len(self.audit), before)

    def test_body_is_strict_and_cannot_supply_commands_or_launch_arguments(self):
        cases = [[], {**self.payload, "command": "attack"}, {**self.payload, "args": {}},
                 {**self.payload, "control": "stop"}, {**self.payload, "control": []},
                 {**self.payload, "expected_generation": None}]
        cases.extend({key: value for key, value in self.payload.items() if key != missing}
                     for missing in self.payload)
        before = len(self.audit)
        for payload in cases:
            with self.subTest(payload=payload):
                status, result = self.post(payload)
                self.assertEqual(status, 400)
                self.assertEqual(result["error"], "invalid_request")
        self.assertEqual(len(self.audit), before)

    def test_controls_do_not_create_an_exception_to_one_active_operation(self):
        self.assertEqual(self.post()[0], 202)
        status, result = self.post(
            {"character": "Testmage", "capability": "controller.quick", "expected_generation": "generation-1"},
            route="/v1/session/perform",
        )
        self.assertEqual(status, 400)
        self.assertIn("nonterminal operation", result["detail"])

    def test_interrupted_and_expired_operations_reject_more_controls(self):
        self.runner.interrupt(self.operation.operation_id)
        self.assertEqual(self.post()[0], 400)
        self.clock.now = self.operation.deadline
        self.assertEqual(self.post()[0], 400)

    def test_broker_kill_switch_and_changed_owner_cannot_be_bypassed_by_route(self):
        self.state.owners["movement"] = "go2"
        self.assertEqual(self.post()[0], 400)
        self.state.owners["movement"] = "lab-test-quick"
        self.broker.control(ActionControl(character="Testmage", enabled=False))
        self.assertEqual(self.post()[0], 400)

    def test_cli_uses_real_authenticated_route_once_and_prints_unknown_application(self):
        output = io.StringIO()
        with patch.object(labctl, "_base_url", return_value=self.base_url), \
                patch.object(labctl, "_token", return_value=self.TOKEN), redirect_stdout(output):
            labctl.main(["control", "Testmage", "hold", "--operation-id", self.operation.operation_id,
                         "--expected-generation", "generation-1"])
        result = json.loads(output.getvalue())
        self.assertIsNone(result["applied"])
        self.assertEqual(result["action"]["status"], "confirmation_required")
        controls = [event for event in self.audit if event.get("event") == "action_proposed"
                    and event.get("command", "").startswith("lab-test-quick hold ")]
        self.assertEqual(len(controls), 1)


class ControllerControlCLIValidationTests(unittest.TestCase):
    def test_cli_requires_both_exact_pins_and_a_known_control_before_transport(self):
        for arguments in (["control", "Testmage", "hold"],
                          ["control", "Testmage", "hold", "--operation-id", "op-1"],
                          ["control", "Testmage", "stop", "--operation-id", "op-1",
                           "--expected-generation", "generation-1"]):
            with self.subTest(arguments=arguments), patch.object(labctl, "_post") as post, \
                    redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                labctl.main(arguments)
            post.assert_not_called()
