"""Runtime source-only reranking respects lexical anchors and evidence policy."""
import hashlib
import sqlite3
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from lich_agent_bridge.discovery import rank_discovery, rerank_semantic
from lich_agent_bridge.knowledge import KnowledgeExcerpt
from lich_agent_bridge.passage_index import (
    PassageIndexUnavailable, build_index, query_passages, ranking_passage_bodies,
)
from . import test_research_sources as fixtures


class SemanticOrderingTests(unittest.TestCase):
    def ranked(self, titles, query, scopes=None):
        return rank_discovery([(KnowledgeExcerpt('synthetic', title, query, f'https://example.test/{i}',
                                                revision_id=1), 'mirror_snapshot',
                                scopes[i] if scopes else 'reference')
                               for i, title in enumerate(titles)], query.split())

    def score(self, ranked, values):
        return {(item.excerpt.source, 1): values.get(item.excerpt.title, 0) for item in ranked}

    def test_explicit_noun_retained_over_broad_guide(self):
        query = 'how does my cobalt talisman combine with worn equipment'
        ranked = self.ranked(['Cobalt talisman', 'Complete Equipment Guide', 'Other rules'], query)
        result = rerank_semantic(ranked, query, self.score(ranked, {'Complete Equipment Guide': 1}))
        self.assertEqual([item.excerpt.title for item in result][:2],
                         ['Cobalt talisman', 'Complete Equipment Guide'])

    def test_partial_name_not_protected_and_numbered_title_is(self):
        query = 'compare 432 equipment cobalt resonance'
        ranked = self.ranked(['Cobalt Equipment Guide', 'Resonance (432)', 'Independent rules'], query)
        result = rerank_semantic(ranked, query, self.score(ranked, {'Independent rules': 1}))
        self.assertEqual([item.excerpt.title for item in result][:2], ['Resonance (432)', 'Independent rules'])

    def test_catalog_not_pinned_and_unscored_scopes_keep_slots(self):
        query = 'synthetic shop equipment rules'
        ranked = self.ranked(['Synthetic Shop', 'Other equipment', 'Character notes'], query,
                             ['reference', 'reference', 'character'])
        positions = [i for i, item in enumerate(ranked) if item.scope == 'character']
        result = rerank_semantic(ranked, query, self.score(ranked, {'Other equipment': 1}))
        self.assertEqual([i for i, item in enumerate(result) if item.scope == 'character'], positions)
        self.assertLess(next(i for i, v in enumerate(result) if v.excerpt.title == 'Other equipment'),
                        next(i for i, v in enumerate(result) if v.excerpt.title == 'Synthetic Shop'))

    def test_equal_scores_preserve_lexical_order(self):
        ranked = self.ranked(['One guide', 'Two guide'], 'equipment rules')
        self.assertEqual(rerank_semantic(ranked, 'equipment rules', self.score(ranked, {})), ranked)

    def test_names_with_stopwords_use_original_query(self):
        from lich_agent_bridge.knowledge import _terms
        query = 'How does the Symbol of Cobalt work?'
        ranked = rank_discovery([(KnowledgeExcerpt('synthetic', title, 'cobalt symbol', title,
                                                   revision_id=1), 'mirror_snapshot', 'reference')
                                 for title in ['Symbol of Cobalt', 'Other guide']], _terms(query))
        result = rerank_semantic(ranked, query, self.score(ranked, {'Other guide': 1}))
        self.assertEqual(result[0].excerpt.title, 'Symbol of Cobalt')


class SemanticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.mirror('== Equipment details ==\nCobalt retains twelve charges after rinsing.')
        with sqlite3.connect(self.fixture.database) as connection:
            build_index(connection)
            self.hits = query_passages(connection, ('cobalt',))

    def test_disabled_path_does_not_read_semantic_inputs(self):
        with patch('lich_agent_bridge.passage_index.ranking_passage_bodies', side_effect=AssertionError('disabled')):
            result = self.fixture.knowledge().open_research(character='Example').search('cobalt charges')
        self.assertTrue(result['data']['items'])
        self.assertFalse(any(row['source'] == 'semantic_reranking' for row in result['diagnostics']))

    def test_enabled_path_keeps_read_handles_freshness_and_database_unchanged(self):
        seen = []
        def score(query, passages, check):
            check()
            seen.extend(passages)
            return SimpleNamespace(scores={p.key: .5 for p in passages}, stats={'windows': len(passages)})
        provider = SimpleNamespace(score=score)
        before = hashlib.sha256(self.fixture.database.read_bytes()).hexdigest()
        session = self.fixture.knowledge(semantic_reranker=provider).open_research(character='Example')
        with patch('lich_agent_bridge.discovery.rerank_semantic', wraps=rerank_semantic) as ranker:
            result = session.search('How do the cobalt charges work?')
        self.assertEqual(ranker.call_args.args[1], 'How do the cobalt charges work?')
        self.assertEqual(result['diagnostics'][-1]['source'], 'semantic_reranking')
        self.assertEqual(result['diagnostics'][-1]['status'], 'success')
        item = result['data']['items'][0]
        read = session.read(item['source_id'])
        self.assertIn('twelve charges', read['data']['text'])
        self.assertTrue(seen)
        self.assertEqual(hashlib.sha256(self.fixture.database.read_bytes()).hexdigest(), before)

    def test_unavailable_backend_retains_same_lexical_items(self):
        from lich_agent_bridge.semantic import SemanticUnavailable
        off = self.fixture.knowledge().open_research(character='Example').search('cobalt charges')
        provider = Mock()
        provider.score.side_effect = SemanticUnavailable('missing_dependencies', 'Optional backend unavailable')
        on = self.fixture.knowledge(semantic_reranker=provider).open_research(character='Example').search('cobalt charges')
        self.assertEqual([i['title'] for i in off['data']['items']], [i['title'] for i in on['data']['items']])
        self.assertTrue(any(d.get('status') == 'missing_dependencies' for d in on['diagnostics']))

    def test_cancellation_is_not_reported_as_lexical_fallback(self):
        provider = Mock()
        session = self.fixture.knowledge(semantic_reranker=provider).open_research(character='Example')
        def score(query, passages, check):
            session.close()
            check()
        provider.score.side_effect = score
        with self.assertRaisesRegex(ValueError, 'closed'):
            session.search('cobalt charges')

    def test_bounded_range_reader_rejects_changed_snapshot_and_caps(self):
        with sqlite3.connect(self.fixture.database) as connection:
            connection.execute('BEGIN')
            bodies = ranking_passage_bodies(connection, self.hits)
            self.assertIn('twelve charges', ''.join(bodies))
            with self.assertRaises(PassageIndexUnavailable):
                ranking_passage_bodies(connection, (replace(self.hits[0], revision_id=999),))
            with self.assertRaises(PassageIndexUnavailable):
                ranking_passage_bodies(connection, (self.hits[0],) * 37)
            with self.assertRaises(PassageIndexUnavailable):
                ranking_passage_bodies(connection, (replace(self.hits[0], end=200_001),))

    def test_snapshot_failure_never_calls_encoder(self):
        provider = Mock()
        with patch('lich_agent_bridge.passage_index.ranking_passage_bodies',
                   side_effect=PassageIndexUnavailable('changed')):
            result = self.fixture.knowledge(semantic_reranker=provider).open_research(character='Example').search('cobalt')
        provider.score.assert_not_called()
        self.assertTrue(any(d.get('status') == 'input_unavailable' for d in result['diagnostics']))
