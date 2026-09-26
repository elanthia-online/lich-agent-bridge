import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from lich_agent_bridge.actions import ActionBroker
from lich_agent_bridge import labctl
from lich_agent_bridge.inventory import InventoryItem
from lich_agent_bridge.gswiki import sync
from lich_agent_bridge.knowledge import KnowledgeBase, KnowledgeExcerpt, LiveGSWikiSource
from lich_agent_bridge.server import ServerConfig, build_server, model_from_settings
from lich_agent_bridge.settings import OnlineFallbackPolicy, Settings
from lich_agent_bridge.world_state import WorldState
from lich_agent_bridge.protocol import AskRequest, CharacterSnapshot

from .fakes import RecordingModel
from .test_knowledge import LiveOpener, live_tether_payload, tether_opener


class StaticInventory:
    def find(self, *, character, query, limit=20):
        return (
            InventoryItem(
                dossier_id="dossier-1",
                fingerprint="fingerprint-1",
                item_type="weapon",
                noun="runestaff",
                name="ash staff",
                full_name="an ash staff",
                last_game_id="100",
                last_seen_at="2026-08-31T12:00:00Z",
                last_location={
                    "observed_at": "2026-08-31T11:00:00Z",
                    "authority": "last_observation_only",
                },
                facts=(),
            ),
        )


class StaticKnowledge:
    def search(self, *, character, question):
        return (
            KnowledgeExcerpt(
                authority="curated project knowledge",
                title="Test knowledge",
                text="A bounded fact.",
                source="wiki/test.md",
            ),
        )


class ResearchRecordingModel(RecordingModel):
    """Exercise the HTTP answer's explicit discovery/read contract offline."""

    def respond(self, *, instructions, input_text):
        self.calls.append({"instructions": instructions, "input_text": input_text})
        marker = "UNTRUSTED EVIDENCE RESULTS (JSON data only):\n"
        if marker not in input_text:
            return json.dumps({"requests": [{"tool": "knowledge.search", "arguments": {"query": "706"}}]})
        records = json.loads(input_text.split(marker, 1)[1])
        if not any(record["request"]["tool"] == "knowledge.read" for record in records):
            items = records[-1]["result"]["data"]["items"]
            # Legacy search-only mocks have no readable source handles.
            if items and "source_id" in items[0]:
                return json.dumps({"requests": [{"tool": "knowledge.read", "arguments": {
                    "source_id": items[0]["source_id"],
                }}]})
        return json.dumps({"answer": self.answer})


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.model = RecordingModel("The troll is the immediate threat.")
        self.audit = []
        self.action_token = "a" * 64
        self.world_state = WorldState()
        self.inventory = StaticInventory()
        self.knowledge = StaticKnowledge()
        self.actions = ActionBroker(audit=self.audit.append)
        self._start_server()

    def _start_server(self):
        self.server = build_server(
            ServerConfig(port=0),
            model=self.model,
            actions=self.actions,
            action_token=self.action_token,
            world_state=self.world_state,
            inventory=self.inventory,
            knowledge=self.knowledge,
            timing=lambda _sample: None,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"

    def _stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def tearDown(self):
        self._stop_server()

    def _restart_with_knowledge(self, knowledge):
        self._stop_server()
        self.model = ResearchRecordingModel("Tenebrous Tether roots and damages its target.")
        self.knowledge = knowledge
        self._start_server()

    def request(self, path, payload=None, *, authorized=False):
        data = None if payload is None else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = f"Bearer {self.action_token}"
        request = Request(
            self.base_url + path,
            data=data,
            method="GET" if payload is None else "POST",
            headers=headers,
        )
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def admit_generation(self, character="Testscout", generation="generation-1"):
        return self.request(
            "/v1/state",
            {
                "character": character,
                "generation": generation,
                "sequence": 1,
                "observed_at": "2026-08-31T12:00:00Z",
                "room": {"id": "8667"},
            },
            authorized=True,
        )

    def test_http_ask_read_only_is_bound_to_selected_session(self):
        self.admit_generation()
        payload = {'character': 'Testscout', 'question': 'What is known?',
                   'read_only': True, 'expected_generation': 'generation-1'}
        status, answer = self.request('/v1/ask', payload, authorized=True)
        self.assertEqual(status, 200)
        self.assertEqual(answer['capability'], 'read_only')
        calls = len(self.model.calls)
        self.admit_generation(generation='generation-2')
        with self.assertRaises(HTTPError) as caught:
            self.request('/v1/ask', payload, authorized=True)
        self.assertEqual(caught.exception.code, 409)
        self.assertEqual(json.load(caught.exception)['error'], 'question_invalidated')
        self.assertEqual(len(self.model.calls), calls)

    def test_question_cli_uses_real_authenticated_http_answer_path(self):
        self.request('/v1/state', {
            'character': 'Testscout', 'generation': 'cli-session', 'sequence': 1,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'room': {'id': '100'},
        }, authorized=True)
        host, port = self.server.server_address
        settings = SimpleNamespace(server=SimpleNamespace(host=host, port=port))
        output = io.StringIO()
        with (mock.patch.object(labctl.Settings, 'load', return_value=settings),
              mock.patch.object(labctl, '_token', return_value=self.action_token),
              redirect_stdout(output)):
            labctl.main(['ask', 'Testscout', 'What is dangerous?'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'answered')
        self.assertEqual(result['answer'], 'The troll is the immediate threat.')
        self.assertEqual(result['generation'], 'cli-session')
        self.assertTrue(result['read_only'])
        self.assertEqual(len(self.model.calls), 1)
        self.assertNotIn(self.action_token, output.getvalue())

    def test_bridge_shaped_observe_then_ask(self):
        status, observed = self.request(
            "/v1/observe",
            {
                "events": [
                    {
                        "timestamp": "2026-08-30T12:00:00Z",
                        "character": "Testscout",
                        "text": "A massive troll king arrives.",
                        "room_id": "100",
                    }
                ]
            },
            authorized=True,
        )
        self.assertEqual(status, 202)
        self.assertEqual(observed["accepted"], 1)

        status, answer = self.request(
            "/v1/ask",
            {"character": "Testscout", "question": "What is dangerous?"},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(answer["capability"], "evidence_gathering")
        self.assertEqual(answer["observed_event_count"], 1)
        self.assertNotIn("command", answer)
        # A direct answer does not silently search or claim reference material.
        self.assertEqual(answer["sources"], [])
        self.assertNotIn("A bounded fact.", self.model.calls[-1]["input_text"])

    def test_health_exposes_the_actual_question_deadline(self):
        self._stop_server()
        self.model.timeout_seconds = 180
        self._start_server()
        status, health = self.request('/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['ask_timeout_seconds'], 180)

    def test_concurrent_generation_publications_fence_dialogue_in_reducer_order(self):
        first_published = threading.Event()
        release_first = threading.Event()
        second_done = threading.Event()
        original = self.world_state.publish_snapshot
        def publish(snapshot):
            result = original(snapshot)
            if snapshot.generation == 'first':
                first_published.set()
                release_first.wait(2)
            return result
        self.world_state.publish_snapshot = publish
        def snapshot(generation):
            return CharacterSnapshot.from_mapping({
                'character': 'Testmage', 'generation': generation, 'sequence': 1,
                'observed_at': '2026-09-06T12:00:00Z',
            })
        first = threading.Thread(target=self.server.publish_snapshot, args=(snapshot('first'),))
        def second_publish():
            self.server.publish_snapshot(snapshot('second'))
            second_done.set()
        second = threading.Thread(target=second_publish)
        first.start()
        try:
            self.assertTrue(first_published.wait(1))
            second.start()
            self.assertFalse(second_done.wait(0.03))
        finally:
            release_first.set()
            first.join(2)
            second.join(2)
        self.assertTrue(second_done.is_set())
        self.assertEqual(self.server.copilot._generations['testmage'], 'second')
        self.assertEqual(self.world_state.snapshot('Testmage')['snapshot']['generation'], 'second')

    def test_question_cannot_mix_new_state_with_old_generation_dialogue(self):
        self.admit_generation('Testmage', 'old')
        self.server.copilot.ask(AskRequest('Testmage', 'old private subject'))
        published = threading.Event()
        release = threading.Event()
        answered = threading.Event()
        original = self.world_state.publish_snapshot
        def publish(snapshot):
            result = original(snapshot)
            published.set()
            release.wait(2)
            return result
        self.world_state.publish_snapshot = publish
        snapshot = CharacterSnapshot.from_mapping({
            'character': 'Testmage', 'generation': 'new', 'sequence': 1,
            'observed_at': '2026-09-06T12:00:00Z',
        })
        state_thread = threading.Thread(target=self.server.publish_snapshot, args=(snapshot,))
        def ask():
            self.server.copilot.ask(AskRequest('Testmage', 'Why?'))
            answered.set()
        ask_thread = threading.Thread(target=ask)
        state_thread.start()
        try:
            self.assertTrue(published.wait(1))
            ask_thread.start()
            self.assertFalse(answered.wait(0.03))
        finally:
            release.set()
            state_thread.join(2)
            ask_thread.join(2)
        self.assertTrue(answered.is_set())
        self.assertNotIn('old private subject', self.model.calls[-1]['input_text'])

    def test_busy_and_forget_have_explicit_http_results_while_question_runs(self):
        entered = threading.Event()
        release = threading.Event()
        def respond(**kwargs):
            entered.set()
            release.wait(3)
            return 'late answer'
        self.model.respond = respond
        errors = []
        def ask():
            try:
                self.request('/v1/ask', {'character': 'Testmage', 'question': 'Why?'}, authorized=True)
            except HTTPError as error:
                errors.append((error.code, json.loads(error.read())))
        worker = threading.Thread(target=ask)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            with self.assertRaises(HTTPError) as busy:
                self.request('/v1/ask', {'character': 'Testmage', 'question': 'Again?'}, authorized=True)
            self.assertEqual(busy.exception.code, 409)
            self.assertEqual(json.loads(busy.exception.read())['error'], 'question_busy')
            self.request('/v1/session/forget', {'character': 'Testmage'}, authorized=True)
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors[0][0], 409)
            self.assertEqual(errors[0][1]['error'], 'question_invalidated')
            _, context = self.request('/v1/session/context', {'character': 'Testmage'}, authorized=True)
            self.assertEqual(context['dialogue_turns'], 0)
        finally:
            release.set()
            worker.join(2)

    def test_real_mirror_and_fake_live_fallback_flow_through_http_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wiki = root / "wiki"
            wiki.mkdir()
            database = root / "gswiki.sqlite3"
            sync(database, namespaces=(0,), delay=0, opener=tether_opener)
            opener = LiveOpener(live_tether_payload())
            live = LiveGSWikiSource(
                cache_path=None,
                opener=opener,
                min_request_interval_seconds=0,
            )
            knowledge = KnowledgeBase(
                wiki_root=wiki,
                gswiki_database=database,
                online_fallback=OnlineFallbackPolicy.WHEN_NEEDED,
                live_gswiki=live,
            )
            self._restart_with_knowledge(knowledge)
            request = {
                "character": "Testmage",
                "question": "what are the updated mechanics of 706?",
            }

            self.assertEqual(self.request("/v1/ask", request, authorized=True)[0], 200)
            status, local_sources = self.request(
                "/v1/session/sources", {"character": "Testmage"}, authorized=True
            )

            self.assertEqual(status, 200)
            self.assertTrue(local_sources["answer_available"])
            local_read = next(source for source in local_sources["sources"]
                              if source.get("evidence_kind") == "read")
            self.assertEqual(
                local_read["title"], "Tenebrous Tether (706)"
            )
            self.assertEqual(
                local_read["authority"],
                "external GSWiki reference — read passage",
            )
            self.assertEqual(local_read["revision_id"], 7061)
            self.assertTrue(local_read["complete"])
            self.assertGreater(local_read["end"], local_read["start"])
            self.assertEqual(
                {
                    item["source"]: item["status"]
                    for item in local_sources["diagnostics"]
                },
                {
                    "curated_wiki": "empty",
                    "local_gswiki": "success",
                },
            )
            self.assertIn("immobilizes a target", self.model.calls[-1]["input_text"])
            self.assertNotIn("immobilizes a target", self.model.calls[0]["input_text"])
            self.assertEqual(len(self.model.calls), 3)
            self.assertEqual(opener.calls, [])

            database.unlink()
            self.assertEqual(self.request("/v1/ask", request, authorized=True)[0], 200)
            status, live_sources = self.request(
                "/v1/session/sources", {"character": "Testmage"}, authorized=True
            )

            self.assertEqual(status, 200)
            live_read = next(source for source in live_sources["sources"]
                             if source.get("evidence_kind") == "read")
            self.assertEqual(
                live_read["title"], "Tenebrous Tether (706)"
            )
            self.assertEqual(
                live_read["authority"], "live GSWiki API — read passage"
            )
            self.assertEqual(live_read["revision_id"], 7061)
            self.assertTrue(live_read["complete"])
            self.assertEqual(len(self.model.calls), 6)
            self.assertEqual(
                {
                    item["source"]: item["status"]
                    for item in live_sources["diagnostics"]
                },
                {
                    "curated_wiki": "empty",
                    "local_gswiki": "missing",
                    "live_gswiki": "success",
                    "general_web": "disabled",
                },
            )
            self.assertIn(
                "Tenebrous Tether has updated mechanics.",
                self.model.calls[-1]["input_text"],
            )
            self.assertEqual(len(opener.calls), 1)

    def test_private_source_context_and_forget_routes_are_character_scoped(self):
        self._restart_with_knowledge(self.knowledge)
        status, before = self.request(
            "/v1/session/sources", {"character": "Testscout"}, authorized=True
        )
        self.assertEqual(status, 200)
        self.assertFalse(before["answer_available"])

        self.request(
            "/v1/ask",
            {"character": "Testscout", "question": "What does the test know?"},
            authorized=True,
        )
        status, sources = self.request(
            "/v1/session/sources", {"character": "Testscout"}, authorized=True
        )
        self.assertEqual(status, 200)
        self.assertTrue(sources["answer_available"])
        self.assertEqual(sources["sources"][0]["title"], "Test knowledge")
        self.assertNotIn("text", sources["sources"][0])

        status, context = self.request(
            "/v1/session/context", {"character": "Testscout"}, authorized=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(context["dialogue_turns"], 1)
        self.assertIn("reference_knowledge", context["context_categories"])

        status, forgotten = self.request(
            "/v1/session/forget", {"character": "Testscout"}, authorized=True
        )
        self.assertEqual(status, 200)
        self.assertTrue(forgotten["forgotten"])
        self.assertFalse(
            self.request(
                "/v1/session/sources", {"character": "Testscout"}, authorized=True
            )[1]["answer_available"]
        )

    def test_health_declares_read_only_capability(self):
        status, health = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["service"], "lich-agent-bridge")
        self.assertEqual(health["version"], "0.2.0")
        self.assertEqual(health["capability"], "confirmed_actions")
        self.assertEqual(health["backend"], "recording-test")
        self.assertTrue(health["model_configured"])

    def test_authenticated_action_round_trip(self):
        self.admit_generation()
        status, control = self.request(
            "/v1/actions/control",
            {"character": "Testscout", "generation": "generation-1", "enabled": True},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertTrue(control["enabled"])

        status, proposed = self.request(
            "/v1/actions/propose",
            {"character": "Testscout", "command": "ready list"},
            authorized=True,
        )
        self.assertEqual(status, 201)
        self.assertEqual(proposed["status"], "queued")

        status, polled = self.request(
            "/v1/actions/poll",
            {"character": "Testscout", "room_id": "8667", "generation": "generation-1"},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(polled["action"]["action_id"], proposed["action_id"])
        self.assertEqual(polled["action"]["status"], "execute")

        status, completed = self.request(
            "/v1/actions/result",
            {
                "action_id": proposed["action_id"],
                "character": "Testscout",
                "generation": "generation-1",
                "outcome": "failed",
                "detail": "combat owner did not start",
            },
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(completed["status"], "failed")

        status, observed = self.request(
            "/v1/actions/status",
            {"action_id": proposed["action_id"]},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(observed["status"], "failed")
        self.assertEqual(observed["detail"], "combat owner did not start")

    def test_blocking_action_delivery_wakes_when_proposed(self):
        self.admit_generation()
        self.request(
            "/v1/actions/control",
            {"character": "Testscout", "generation": "generation-1", "enabled": True},
            authorized=True,
        )
        started = threading.Event()
        received = {}

        def wait_for_action():
            started.set()
            status, body = self.request(
                "/v1/actions/next",
                {
                    "character": "Testscout",
                    "room_id": "8667",
                    "generation": "generation-1",
                    "timeout_seconds": 1,
                },
                authorized=True,
            )
            received.update(status=status, body=body)

        thread = threading.Thread(target=wait_for_action)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        _, proposed = self.request(
            "/v1/actions/propose",
            {"character": "Testscout", "command": "ready list"},
            authorized=True,
        )
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(received["status"], 200)
        self.assertEqual(received["body"]["action"]["action_id"], proposed["action_id"])

    def test_second_bridge_generation_rejects_stale_action_lifecycle(self):
        self.admit_generation(generation="bridge-a")
        self.request(
            "/v1/actions/control",
            {"character": "Testscout", "generation": "bridge-a", "enabled": True},
            authorized=True,
        )
        _, pending = self.request(
            "/v1/actions/propose",
            {"character": "Testscout", "command": "north"},
            authorized=True,
        )
        self.assertEqual(pending["generation"], "bridge-a")

        self.request(
            "/v1/state",
            {
                "character": "Testscout",
                "generation": "bridge-b",
                "sequence": 1,
                "observed_at": "2026-08-31T12:01:00Z",
                "room": {"id": "8667"},
            },
            authorized=True,
        )
        with self.assertRaises(HTTPError) as stale:
            self.request(
                "/v1/actions/next",
                {
                    "character": "Testscout",
                    "room_id": "8667",
                    "generation": "bridge-a",
                    "timeout_seconds": 0,
                },
                authorized=True,
            )
        self.assertEqual(stale.exception.code, 400)

        status, action = self.request(
            "/v1/actions/next",
            {
                "character": "Testscout",
                "room_id": "8667",
                "generation": "bridge-b",
                "timeout_seconds": 0,
            },
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertIsNone(action["action"])
        self.assertEqual(self.actions.get(pending["action_id"])["status"], "cancelled")

    def test_action_routes_require_token(self):
        with self.assertRaises(HTTPError) as caught:
            self.request(
                "/v1/actions/control",
                {"character": "Testscout", "generation": "generation-1", "enabled": True},
            )
        self.assertEqual(caught.exception.code, 401)

    def test_observe_ask_and_state_routes_require_token(self):
        private_requests = [
            (
                "/v1/observe",
                {
                    "events": [
                        {
                            "timestamp": "2026-08-31T12:00:00Z",
                            "character": "Testscout",
                            "text": "A troll arrives.",
                        }
                    ]
                },
            ),
            (
                "/v1/ask",
                {"character": "Testscout", "question": "What happened?"},
            ),
            (
                "/v1/state",
                {
                    "character": "Testscout",
                    "generation": "generation-1",
                    "sequence": 1,
                    "observed_at": "2026-08-31T12:00:00Z",
                },
            ),
            (
                "/v1/event",
                {
                    "character": "Testscout",
                    "generation": "generation-1",
                    "observed_at": "2026-08-31T12:00:00Z",
                    "kind": "threat",
                    "summary": "A troll arrived.",
                },
            ),
            ("/v1/session/snapshot", {"character": "Testscout"}),
            ("/v1/session/capabilities", {}),
            ("/v1/session/sources", {"character": "Testscout"}),
            ("/v1/session/context", {"character": "Testscout"}),
            ("/v1/session/forget", {"character": "Testscout"}),
            ("/v1/state/Testscout", None),
            ("/v1/watch/Testscout?cursor=0&timeout=0", None),
        ]
        for path, payload in private_requests:
            with self.subTest(path=path), self.assertRaises(HTTPError) as caught:
                self.request(path, payload)
            self.assertEqual(caught.exception.code, 401)
        self.assertEqual(self.model.calls, [])

    def test_state_publish_and_query_do_not_call_game_or_model(self):
        status, published = self.request(
            "/v1/state",
            {
                "character": "Testscout",
                "generation": "generation-1",
                "sequence": 1,
                "observed_at": "2026-08-31T12:00:00Z",
                "room": {"id": "100", "title": "Town Well"},
                "vitals": {"health": {"current": 80, "max": 100}},
            },
            authorized=True,
        )
        self.assertEqual(status, 202)
        self.assertTrue(published["accepted"])

        status, current = self.request("/v1/state/testscout", authorized=True)
        self.assertEqual(status, 200)
        self.assertEqual(current["snapshot"]["room"]["id"], "100")
        self.assertIn("age_seconds", current["freshness"])
        self.assertEqual(self.model.calls, [])

    def test_state_conflicts_return_409(self):
        base = {
            "character": "Testscout",
            "generation": "generation-1",
            "sequence": 2,
            "observed_at": "2026-08-31T12:00:00Z",
        }
        self.request("/v1/state", base, authorized=True)
        with self.assertRaises(HTTPError) as caught:
            self.request("/v1/state", base, authorized=True)
        self.assertEqual(caught.exception.code, 409)

    def test_watch_route_wakes_on_event_and_times_out(self):
        _, published = self.request(
            "/v1/state",
            {
                "character": "Testscout",
                "generation": "generation-1",
                "sequence": 1,
                "observed_at": "2026-08-31T12:00:00Z",
            },
            authorized=True,
        )
        started = threading.Event()
        watched = {}

        def watch():
            started.set()
            status, page = self.request(
                f"/v1/watch/Testscout?cursor={published['cursor']}&timeout=1",
                authorized=True,
            )
            watched.update(status=status, page=page)

        thread = threading.Thread(target=watch)
        thread.start()
        self.assertTrue(started.wait(timeout=1))
        _, event = self.request(
            "/v1/event",
            {
                "character": "Testscout",
                "generation": "generation-1",
                "observed_at": "2026-08-31T12:00:01Z",
                "kind": "threat",
                "summary": "A troll arrived.",
            },
            authorized=True,
        )
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(watched["status"], 200)
        self.assertEqual(watched["page"]["cursor"], event["cursor"])
        self.assertEqual(watched["page"]["events"][0]["kind"], "threat")

        status, timed_out = self.request(
            f"/v1/watch/Testscout?cursor={event['cursor']}&timeout=0",
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertTrue(timed_out["timed_out"])

    def test_build_server_exposes_one_shared_world_state(self):
        self.assertIs(self.server.world_state, self.world_state)
        self.assertIs(self.server.session_hub.world_state, self.world_state)
        self.assertIs(self.server.actions, self.actions)
        self.assertIs(self.server.session_hub.actions, self.actions)
        self.assertIs(self.server.session_hub.inventory, self.inventory)
        self.assertIs(self.server.session_hub.knowledge, self.knowledge)

    def test_session_read_routes_use_compact_envelopes_without_model_or_action(self):
        self.request(
            "/v1/state",
            {
                "character": "Testmage",
                "generation": "generation-1",
                "sequence": 1,
                "observed_at": "2026-08-31T12:00:00Z",
                "room": {"id": "1234", "title": "Town Well"},
                "hands": {
                    "right": {"id": "100", "name": "an ash staff"},
                    "left": None,
                },
            },
            authorized=True,
        )
        status, snapshot = self.request(
            "/v1/session/snapshot", {"character": "Testmage"}, authorized=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["room"]["id"], "1234")
        self.assertIsInstance(snapshot["cursor"], str)

        status, watched = self.request(
            "/v1/session/watch",
            {"character": "Testmage", "cursor": "0", "timeout_ms": 0},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertGreaterEqual(watched["total"], 1)
        self.assertTrue(any(item["kind"] == "snapshot" for item in watched["items"]))

        status, inventory = self.request(
            "/v1/session/inventory/find",
            {"character": "Testmage", "query": "staff"},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(inventory["total"], 1)
        self.assertEqual(
            inventory["items"][0]["last_location"]["authority"],
            "last_observation_only",
        )

        status, wiki = self.request(
            "/v1/session/wiki/search",
            {"query": "ensorcell", "character": "Testmage", "limit": 1},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(wiki["total"], 1)
        self.assertEqual(wiki["items"][0]["source"], "wiki/test.md")

        status, capabilities = self.request(
            "/v1/session/capabilities",
            {"character": "Testmage"},
            authorized=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(capabilities["total"], 7)
        self.assertEqual({item["name"] for item in capabilities["items"]},
                         {"travel.go2", "character.recon", "item.audit", "hunt.prepare", "room.loot",
                          "session.recover_controller", "session.command"})
        self.assertFalse(
            next(
                item
                for item in capabilities["items"]
                if item["name"] == "hunt.prepare"
            )["available"]
        )
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.audit, [])

    def test_session_perform_route_returns_id_then_truthful_terminal_failure(self):
        status, result = self.request(
            "/v1/session/perform",
            {"character": "Testmage", "capability": "room.loot"},
            authorized=True,
        )
        self.assertEqual(status, 202)
        self.assertTrue(result["operation_id"])
        status, watched = self.request(
            "/v1/session/operation/watch",
            {
                "operation_id": result["operation_id"],
                "cursor": "0",
                "timeout_ms": 1000,
            },
            authorized=True,
        )
        self.assertEqual(status, 200)
        operation = watched["operation"]
        self.assertEqual(operation["status"], "failed")
        self.assertIn("live snapshot is unavailable", operation["explanation"])
        self.assertTrue(operation["progress"])
        self.assertEqual(self.audit, [])


class RuntimeSettingsTests(unittest.TestCase):
    def test_one_loaded_settings_object_drives_runtime_composition(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "config.toml"
            manifest = (
                Path(__file__).resolve().parents[1]
                / "lich"
                / "lab-controllers.json"
            )
            config.write_text(
                f'''schema_version = 1

[server]
host = "127.0.0.1"
port = 19444

[knowledge]
wiki_root = "knowledge"
gswiki_database = "data/wiki.sqlite3"

[storage]
state_directory = "state"
inventory_database = "data/items.sqlite3"
action_token_file = "state/token"
audit_log = "state/audit.jsonl"
controller_manifest = {json.dumps(str(manifest))}

[providers.codex]
command = "/configured/codex"

[profiles.default]
model = "configured-model"
timeout_seconds = 17
''',
                encoding="utf-8",
            )
            environment = {
                "HOME": str(root),
                "LAB_PORT": "19555",
                "LAB_CODEX_BIN": "/environment/codex",
                "LAB_CODEX_MODEL": "environment-model",
            }
            resolved = Settings.load(config, environment=environment)

            with (
                mock.patch.object(Settings, "load", return_value=resolved) as loader,
                mock.patch("lich_agent_bridge.server.LabHTTPServer") as server_type,
            ):
                built = build_server()

            loader.assert_called_once_with()
            self.assertIs(built, server_type.return_value)
            (
                address,
                copilot,
                broker,
                _token,
                _world_state,
                hub,
                _watchers,
                character_knowledge,
            ) = server_type.call_args.args
            self.assertEqual(address, ("127.0.0.1", 19555))
            self.assertEqual(copilot._model._binary, "/environment/codex")
            self.assertEqual(copilot._model._model, "environment-model")
            self.assertEqual(copilot._model._timeout, 17.0)
            self.assertEqual(hub.knowledge._wiki_root, root / "knowledge")
            self.assertEqual(
                hub.inventory.database, root / "data" / "items.sqlite3"
            )
            self.assertEqual(character_knowledge.database, hub.inventory.database)
            self.assertEqual(broker._audit.path, root / "state" / "audit.jsonl")
            self.assertEqual(
                copilot._timing.path, root / "state" / "timings.jsonl"
            )
            self.assertTrue((root / "state" / "token").is_file())
            self.assertIs(
                broker._policy._controller_manifest,
                hub.capabilities._controller_manifest,
            )

    def test_openai_credential_uses_only_configured_environment_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            environment = {
                "HOME": str(root),
                "PRIVATE_LAB_KEY": "configured-secret",
                "OPENAI_API_KEY": "unrelated-secret",
            }
            settings = Settings.load(
                environment=environment,
                overrides={
                    "selected_profile": "private",
                    "providers": {
                        "private": {
                            "kind": "openai",
                            "base_url": "https://example.test/v1",
                            "credential_env": "PRIVATE_LAB_KEY",
                        }
                    },
                    "profiles": {
                        "private": {
                            "provider": "private",
                            "model": "configured-model",
                            "reasoning_effort": "high",
                            "timeout_seconds": 11,
                            "web_search": False,
                        }
                    },
                },
            )

            with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "ambient-secret"}):
                model = model_from_settings(settings, environment=environment)

            self.assertEqual(model._api_key, "configured-secret")
            self.assertEqual(model._model, "configured-model")
            self.assertEqual(model._reasoning_effort, "high")
            self.assertEqual(model._base_url, "https://example.test/v1")
            self.assertEqual(model._timeout, 11.0)
            self.assertNotIn("configured-secret", json.dumps(settings.redacted()))
            self.assertNotIn("unrelated-secret", json.dumps(settings.redacted()))


if __name__ == "__main__":
    unittest.main()
