"""Question-to-evidence-to-answer integration with no live model or game."""
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from lich_agent_bridge.actions import ActionApproval, ActionContext, ActionControl, ActionResult
from lich_agent_bridge.character_knowledge import CharacterKnowledge
from lich_agent_bridge.errors import ModelError, QuestionInvalidated, ValidationError
from lich_agent_bridge.protocol import AskRequest, CharacterSnapshot
from lich_agent_bridge.server import ServerConfig, build_server
from .test_character_knowledge import character_snapshot
from .test_server import StaticInventory, StaticKnowledge


class ConfiguredInventory(StaticInventory):
    configured = True


class Turns:
    backend = 'isolated-turns'
    configured = True

    def __init__(self, *turns):
        self.turns = iter(turns)
        self.calls = []

    def respond(self, **options):
        self.calls.append(options)
        value = next(self.turns)
        return value if isinstance(value, str) else json.dumps(value)


def request(tool, **arguments):
    return {'requests': [{'tool': tool, 'arguments': arguments}]}


class EvidenceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = Path(self.directory.name) / 'inventory.sqlite3'
        sqlite3.connect(self.database).close()
        self.commands = []
        self.sequence = 0

    def start(self, model):
        self.model = model
        self.server = build_server(
            ServerConfig(port=0), model=model, action_token='a' * 64,
            inventory=ConfiguredInventory(), knowledge=StaticKnowledge(),
            character_knowledge=CharacterKnowledge(self.database), timing=lambda _sample: None,
        )
        self.addCleanup(self.server.server_close)
        self.server.session_hub.capabilities._step_hook = self.drive

    def publish(self, *, fresh=False, generation='generation-1'):
        self.sequence += 1
        data = character_snapshot().to_mapping()
        stamp = datetime.now(timezone.utc).isoformat()
        data.update(sequence=self.sequence, generation=generation, observed_at=stamp, room={'id': '100'})
        for category in data['character_data'].values():
            category['observed_at'] = stamp
            if not fresh:
                category.update(source='infomon_cache', observed_at=None, complete=False)
        self.server.publish_snapshot(CharacterSnapshot.from_mapping(data))

    def drive(self, broker, proposed, state):
        context = ActionContext(state.character, state.room_id, state.generation)
        action = broker.poll(context)
        if action['status'] == 'confirmation_required':
            broker.approve(ActionApproval(action['action_id'], state.character, state.room_id,
                                          generation=state.generation, approval_mode='auto'))
            action = broker.poll(context)
        self.commands.extend(action.get('commands') or [action['command']])
        self.publish(fresh=True)
        broker.record_result(ActionResult(action['action_id'], state.character, 'completed',
                                          'isolated bridge', generation=state.generation))

    def enable(self):
        self.server.actions.control(ActionControl('Testmage', True, 'generation-1'))

    def test_read_only_question_cannot_refresh_even_with_actions_enabled(self):
        self.start(Turns(request('character.read', freshness='prefer_fresh'),
                         {'answer': 'Only historical observations are available.'}))
        self.publish()
        self.enable()
        answer = self.server.copilot.ask(AskRequest(
            'Testmage', 'Check my current training.', read_only=True,
            expected_generation='generation-1'))
        self.assertEqual(self.commands, [])
        self.assertEqual(answer.capability, 'read_only')
        self.assertIn('read-only', self.model.calls[-1]['input_text'])
        self.assertTrue(any('read-only' in str(diagnostic) for diagnostic in answer.source_diagnostics))
        self.assertEqual(len(self.model.calls), 2)

    def test_bound_question_rejects_unknown_or_replaced_session_before_model(self):
        self.start(Turns({'answer': 'Should not run.'}))
        for generation in ('missing', 'generation-1'):
            if generation == 'generation-1':
                self.publish(generation='generation-2')
            with self.assertRaises(QuestionInvalidated):
                self.server.copilot.ask(AskRequest(
                    'Testmage', 'Training?', read_only=True, expected_generation=generation))
        self.assertEqual(self.model.calls, [])
        self.assertEqual(self.commands, [])

    def test_read_only_question_still_reads_fresh_observations(self):
        self.start(Turns(request('character.read'), {'answer': 'Observed training.'}))
        self.publish(fresh=True)
        answer = self.server.copilot.ask(AskRequest(
            'Testmage', 'Training?', read_only=True, expected_generation='generation-1'))
        self.assertEqual(answer.capability, 'read_only')
        self.assertTrue(any(source.get('authority') == 'current_character_observation'
                            for source in answer.sources))
        self.assertEqual(self.commands, [])

    def test_read_only_mode_does_not_change_the_next_questions_action_policy(self):
        self.start(Turns(request('character.read'), {'answer': 'Historical data.'},
                         request('character.read'), {'answer': 'Fresh data.'}))
        self.publish()
        self.enable()
        self.server.copilot.ask(AskRequest('Testmage', 'Training?', read_only=True))
        self.assertEqual(self.commands, [])
        self.server.copilot.ask(AskRequest('Testmage', 'Refresh training.'))
        self.assertEqual(self.commands, ['info', 'skills'])

    def test_model_selected_refresh_updates_database_and_reaches_next_turn(self):
        self.start(Turns(request('character.read'), {'answer': '30 Sorcerer ranks, freshly observed.'}))
        self.publish()
        self.enable()
        answer = self.server.copilot.ask(AskRequest('Testmage', 'Could this last longer with how I have trained?'))
        self.assertEqual(self.commands, ['info', 'skills'])
        self.assertEqual(answer.capability, 'evidence_gathering')
        self.assertIn('30', self.model.calls[1]['input_text'])
        self.assertIn('current_character_observation', self.model.calls[1]['input_text'])
        stored = self.server.character_knowledge.find(character='Testmage', generation='generation-1')
        self.assertEqual(stored['skills']['values']['spell_circles']['sorcerer'], 30)

    def test_fresh_build_reuse_is_independent_of_question_words(self):
        self.start(Turns(request('character.read'), {'answer': 'Known.'}, request('character.read'), {'answer': 'Still known.'}))
        self.publish(fresh=True)
        self.enable()
        for question in ('Could I do that?', 'What is my lore?'):
            self.server.copilot.ask(AskRequest('Testmage', question))
        self.assertEqual(self.commands, [])
        self.assertEqual(len(self.model.calls), 4)

    def test_actions_off_is_evidence_failure_not_automatic_permission(self):
        self.start(Turns(request('character.read'), {'answer': 'Fresh information unavailable.'}))
        self.publish()
        self.server.copilot.ask(AskRequest('Testmage', 'Would that help?'))
        self.assertEqual(self.commands, [])
        self.assertIn('disabled', self.model.calls[1]['input_text'])
        self.assertEqual(self.server.character_knowledge.find(character='Testmage'), {})

    def test_direct_answer_without_session_needs_one_call_and_no_recon(self):
        self.start(Turns({'answer': 'Hello!'}))
        answer = self.server.copilot.ask(AskRequest('Testmage', 'Hello'))
        self.assertEqual(answer.text, 'Hello!')
        self.assertEqual(len(self.model.calls), 1)
        self.assertEqual(self.commands, [])

    def test_inventory_and_knowledge_requests_reach_same_answer_with_sources(self):
        self.start(Turns({'requests': [
            {'tool': 'inventory.search', 'arguments': {'query': 'staff'}},
            {'tool': 'knowledge.search', 'arguments': {'query': 'staff'}},
        ]}, {'answer': 'Here is the evidence.'}))
        answer = self.server.copilot.ask(AskRequest('Testmage', 'What do you know about this equipment?'))
        self.assertIn('ash staff', self.model.calls[1]['input_text'])
        self.assertIn('wiki/test.md', {item.get('source') for item in answer.sources})
        self.assertEqual(self.commands, [])

    def test_cross_character_request_is_rejected_before_any_refresh(self):
        self.start(Turns(request('character.read', character='Testscout')))
        self.publish()
        self.enable()
        with self.assertRaises(ModelError):
            self.server.copilot.ask(AskRequest('Testmage', 'Help me.'))
        self.assertEqual(self.commands, [])

    def test_forget_while_model_selects_evidence_prevents_tool_execution(self):
        entered, release = threading.Event(), threading.Event()
        model = Turns(request('character.read'))
        original = model.respond
        def respond(**options):
            entered.set()
            release.wait(2)
            return original(**options)
        model.respond = respond
        self.start(model)
        self.publish()
        self.enable()
        errors = []
        def ask():
            try:
                self.server.copilot.ask(AskRequest('Testmage', 'Could I do that?'))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=ask)
        worker.start()
        try:
            self.assertTrue(entered.wait(1))
            self.server.copilot.forget('Testmage')
        finally:
            release.set()
            worker.join(2)
        self.assertTrue(any(isinstance(error, QuestionInvalidated) for error in errors))
        self.assertEqual(self.commands, [])

    def test_forget_revokes_pending_recon_before_blocked_watch_returns(self):
        self._cancel_pending_recon(replace_session=False)

    def test_session_replacement_revokes_pending_recon_before_watch_returns(self):
        self._cancel_pending_recon(replace_session=True)

    def _cancel_pending_recon(self, *, replace_session):
        self.start(Turns(request('character.read')))
        self.publish()
        self.enable()
        watching, offered, release = threading.Event(), threading.Event(), threading.Event()
        pending = []
        def on_submit(broker, action, state):
            pending.append(action['action_id'])
            offered.set()
        self.server.session_hub.capabilities._step_hook = on_submit
        original_watch = self.server.session_hub.watch_operation
        def watch(options):
            watching.set()
            release.wait(3)
            return original_watch(options)
        self.server.session_hub.watch_operation = watch
        errors = []
        def ask():
            try:
                self.server.copilot.ask(AskRequest('Testmage', 'Could I do that?'))
            except Exception as error:
                errors.append(error)
        worker = threading.Thread(target=ask)
        worker.start()
        try:
            self.assertTrue(offered.wait(1))
            self.assertTrue(watching.wait(1))
            if replace_session:
                self.publish(generation='generation-2')
            else:
                self.server.copilot.forget('Testmage')
            self.assertEqual(self.server.actions.get(pending[0])['status'], 'cancelled')
            with self.assertRaises(ValidationError):
                self.server.actions.approve(ActionApproval(
                    pending[0], 'Testmage', '100', generation='generation-1'))
            self.assertEqual(self.commands, [])
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(any(isinstance(error, QuestionInvalidated) for error in errors))
