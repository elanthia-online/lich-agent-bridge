"""Selected-page freshness across independent questions with a shared adapter."""

import json
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

from lich_agent_bridge.knowledge import LiveGSWikiSource
from lich_agent_bridge.settings import OnlineFallbackPolicy
from . import test_research_sources as fixtures


class Response:
    def __init__(self, title="Synthetic Equipment", revision=8):
        self.payload = {"query": {"pages": [{"title": title, "revisions": [{
            "revid": revision, "slots": {"main": {
                "content": "== Rules ==\nSynthetic equipment current rule."}},
        }]}]}}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return json.dumps(self.payload).encode()


class RepeatedQuestionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ResearchSourceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.mirror("== Rules ==\nSynthetic equipment old rule.",
                            stamp="2020-01-01T00:00:00+00:00")
        self.current = datetime(2026, 9, 6, tzinfo=UTC)
        self.ticks = 100.0
        self.opener = Mock(return_value=Response())

    def live(self, *, persistent=True):
        return LiveGSWikiSource(
            cache_path=self.fixture.root / "live-cache.json" if persistent else None,
            opener=self.opener, now=lambda: self.current, monotonic=lambda: self.ticks,
        )

    def knowledge(self, live, *, enabled=True):
        return self.fixture.knowledge(
            online_fallback=(OnlineFallbackPolicy.WHEN_NEEDED if enabled
                             else OnlineFallbackPolicy.DISABLED), live_gswiki=live,
        )

    def selected_read(self, knowledge):
        session = knowledge.open_research(character="Example")
        self.addCleanup(session.close)
        candidate = session.search("synthetic equipment")["data"]["items"][0]
        return session, session.read(candidate["source_id"])

    def test_successful_selected_refresh_is_reused_across_questions(self):
        for persistent in (False, True):
            with self.subTest(persistent=persistent):
                self.opener.reset_mock()
                knowledge = self.knowledge(self.live(persistent=persistent))
                stamp = self.current.isoformat()
                prior_handle = None
                for question in range(3):
                    session, result = self.selected_read(knowledge)
                    self.assertEqual(result["data"]["provenance"]["revision_id"], 8,
                                     f"question {question + 1}: {result['diagnostics']}")
                    self.assertEqual(result["data"]["provenance"]["freshness"],
                                     "verified_at_retrieval")
                    self.assertEqual(result["data"]["provenance"]["retrieved_at"], stamp)
                    self.assertIn("current rule", result["data"]["text"])
                    if prior_handle:
                        with self.assertRaises(ValueError):
                            session.validate_read(prior_handle)
                    prior_handle = result["data"]["source_id"]
                    session.close()
                    self.current += timedelta(seconds=0.2)
                    self.ticks += 0.2
                self.assertEqual(self.opener.call_count, 1)

    def test_persisted_page_keeps_original_retrieval_time(self):
        _, first = self.selected_read(self.knowledge(self.live()))
        self.current += timedelta(seconds=20)
        _, second = self.selected_read(self.knowledge(self.live()))
        self.assertEqual(first["data"]["provenance"], second["data"]["provenance"])
        self.assertEqual(self.opener.call_count, 1)

    def test_newer_disk_entry_supersedes_older_memory_entry(self):
        first = self.live()
        first.read("Synthetic Equipment")
        self.current += timedelta(seconds=901)
        self.ticks += 901
        self.opener.return_value = Response(revision=9)
        self.live().read("Synthetic Equipment")
        excerpt, diagnostic = first.read("Synthetic Equipment")
        self.assertEqual(excerpt.revision_id, 9)
        self.assertEqual(diagnostic.status, "success")
        self.assertEqual(self.opener.call_count, 2)

    def test_expired_page_refresh_failure_stays_stale_and_rate_limited(self):
        knowledge = self.knowledge(self.live())
        self.selected_read(knowledge)
        self.current += timedelta(seconds=901)
        self.ticks += 901
        self.opener.side_effect = TimeoutError()
        _, failed = self.selected_read(knowledge)
        self.assertEqual(failed["data"]["provenance"]["freshness"], "stale")
        self.assertEqual(failed["data"]["provenance"]["revision_id"], 7)
        self.assertIn("old rule", failed["data"]["text"])
        self.assertEqual(failed["diagnostics"][0]["status"], "unavailable")
        _, limited = self.selected_read(knowledge)
        self.assertEqual(limited["data"]["provenance"]["freshness"], "stale")
        self.assertEqual(limited["diagnostics"][0]["status"], "rate_limited")
        self.assertEqual(self.opener.call_count, 2)

    def test_expired_page_can_receive_new_revision(self):
        knowledge = self.knowledge(self.live())
        self.selected_read(knowledge)
        self.current += timedelta(seconds=901)
        self.ticks += 901
        self.opener.return_value = Response(revision=9)
        _, result = self.selected_read(knowledge)
        self.assertEqual(result["data"]["provenance"]["revision_id"], 9)
        self.assertEqual(result["data"]["provenance"]["retrieved_at"], self.current.isoformat())
        self.assertEqual(self.opener.call_count, 2)

    def test_disabled_policy_does_not_use_previously_cached_refresh(self):
        live = self.live()
        self.selected_read(self.knowledge(live))
        _, result = self.selected_read(self.knowledge(live, enabled=False))
        self.assertEqual(result["data"]["provenance"]["freshness"], "stale")
        self.assertEqual(result["data"]["provenance"]["revision_id"], 7)
        self.assertEqual(result["diagnostics"][0]["status"], "disabled")
        self.assertEqual(self.opener.call_count, 1)

    def test_cache_hit_does_not_spend_interval_for_distinct_selected_page(self):
        live = self.live()
        live.read("Synthetic Equipment")
        self.ticks += 1.1
        self.current += timedelta(seconds=1.1)
        _, cached = live.read("Synthetic Equipment")
        self.opener.return_value = Response("Synthetic Saved Post")
        other, status = live.read("Synthetic Saved Post")
        self.assertEqual(cached.status, "success")
        self.assertEqual(status.status, "success")
        self.assertEqual(other.title, "Synthetic Saved Post")
        absent, limited = live.read("Synthetic equipment")
        self.assertIsNone(absent)
        self.assertEqual(limited.status, "rate_limited")
        self.assertEqual(self.opener.call_count, 2)

    def test_empty_page_is_not_cached_as_success(self):
        response = Response()
        response.payload = {"query": {"pages": []}}
        self.opener.return_value = response
        live = self.live()
        first, empty = live.read("Synthetic Equipment")
        second, limited = live.read("Synthetic Equipment")
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual((empty.status, limited.status), ("empty", "rate_limited"))
        self.assertEqual(self.opener.call_count, 1)

    def test_disk_write_failure_retains_bounded_shared_cache(self):
        knowledge = self.knowledge(self.live())
        with patch("lich_agent_bridge.knowledge.os.replace", side_effect=OSError()):
            self.selected_read(knowledge)
        _, result = self.selected_read(knowledge)
        self.assertEqual(result["data"]["provenance"]["revision_id"], 8)
        self.assertEqual(self.opener.call_count, 1)

    def test_page_cache_evicts_at_existing_bound(self):
        live = self.live(persistent=False)
        for index in range(65):
            self.opener.return_value = Response(f"Synthetic Page {index}")
            live.read(f"Synthetic Page {index}")
            self.ticks += 2
            self.current += timedelta(seconds=2)
        live.read("Synthetic Page 64")
        self.assertEqual(self.opener.call_count, 65)
        self.opener.return_value = Response("Synthetic Page 0")
        live.read("Synthetic Page 0")
        self.assertEqual(self.opener.call_count, 66)


if __name__ == "__main__":
    unittest.main()
