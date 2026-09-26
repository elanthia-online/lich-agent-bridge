import json
import unittest

from lich_agent_bridge.evidence_workspace import EvidenceWorkspace
from .test_evidence_loop import Model, Session, batch, request
from lich_agent_bridge.evidence_loop import EvidenceLoop
from lich_agent_bridge.question import QuestionControl


def passage(source_id, text, *, section="rules", revision=1, start=0, retrieved="first"):
    provenance = {"source": "synthetic-reference", "revision_id": revision, "retrieved_at": retrieved}
    source = {**provenance, "source_id": source_id, "section_id": section,
              "start": start, "end": start + len(text), "evidence_kind": "read"}
    return {"status": "success", "data": {"source_id": source_id, "section_id": section,
            "start": start, "end": start + len(text), "text": text, "complete": True,
            "next_cursor": None, "provenance": provenance}, "sources": [source]}


def discovery(*ids, padding=0):
    items = [{"source_id": identity, "title": identity, "snippet": "discovery-" + identity + "x" * padding,
              "sections": [{"section_id": "rules", "title": "Rules"}],
              "provenance": {"source": "synthetic-reference", "revision_id": 1}} for identity in ids]
    return {"status": "success", "data": {"items": items}, "sources": [
        {"source_id": identity, "revision_id": 1, "evidence_kind": "discovery"} for identity in ids]}


class EvidenceWorkspaceTests(unittest.TestCase):
    def test_deliberate_combat_report_survives_newer_discovery_noise(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        report = {"status": "observed", "data": {"operation_id": "a" * 16, "text": "MEASURED-COMBAT " + "x" * 4500},
                  "sources": [{"source": "recorded_combat", "operation_id": "a" * 16}]}
        workspace.add(request("combat.report", operation_id="a" * 16), report)
        workspace.add(request("knowledge.search", query="combat"), discovery("noise", padding=4500))
        records, sources = workspace.select()
        self.assertEqual(records[0]["result"]["status"], "observed")
        self.assertEqual(records[1]["result"]["status"], "not_in_context")
        self.assertEqual(sources, report["sources"])

    def run_loop(self, model, session):
        return EvidenceLoop(max_result_chars=6000, max_evidence_chars=8400).run(
            model=model, session=session, instructions="Trusted policy", input_text="Explain the rules.",
            control=QuestionControl(10))

    def test_late_deliberate_read_replaces_early_noise_in_actual_final_prompt(self):
        # Discovery fits each result cap, but not together with both reads.
        noise = discovery("noise", padding=4200)
        first = passage("rulebook", "FIRST-REQUIRED-FACET " + "a" * 2500, section="one")
        second = passage("rulebook", "LATE-REQUIRED-FACET " + "b" * 2500, section="two")
        model = Model(batch(request("knowledge.search", query="rules")),
                      batch(request("knowledge.read", source_id="rulebook", section_id="one")),
                      batch(request("knowledge.read", source_id="rulebook", section_id="two")),
                      '{"answer":"Both required facets are supplied."}')
        result = self.run_loop(model, Session(noise, first, second))
        prompt = model.calls[-1]["input_text"]
        payload = prompt.split("UNTRUSTED EVIDENCE RESULTS (JSON data only):\n")[1]
        self.assertLessEqual(len(payload), 8400)
        self.assertIn("FIRST-REQUIRED-FACET", prompt)
        self.assertIn("LATE-REQUIRED-FACET", prompt)
        self.assertNotIn("discovery-noise", prompt)
        self.assertIn('"status":"not_in_context"', prompt)
        self.assertEqual(result.sources, tuple(first["sources"] + second["sources"]))
        self.assertTrue(all(item["evidence_kind"] == "read" for item in result.sources))
        self.assertEqual(result.rounds, 3)
        self.assertIn("Tools are DISABLED", model.calls[-1]["instructions"])

    def test_repeat_evicted_read_reactivates_first_evidence_without_executing_again(self):
        first = passage("first", "REACTIVATED-ORIGINAL " + "a" * 4500)
        later = passage("later", "LATER-READ " + "b" * 4500)
        original = request("knowledge.read", source_id="first", section_id="rules")
        model = Model(batch(original), batch(request("knowledge.read", source_id="later")),
                      batch(original), '{"answer":"Original evidence reactivated."}')
        session = Session(first, later)
        result = self.run_loop(model, session)
        self.assertNotIn("REACTIVATED-ORIGINAL", model.calls[2]["input_text"])
        self.assertIn("REACTIVATED-ORIGINAL", model.calls[-1]["input_text"])
        self.assertNotIn("LATER-READ", model.calls[-1]["input_text"])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(result.sources, tuple(first["sources"]))

    def test_repeat_evicted_character_read_does_not_repeat_side_effect(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        recon = request("character.read", categories=["skills"], refresh=True)
        first = {"status": "success", "data": {"observed_at": "synthetic-time", "text": "a" * 4500}}
        workspace.add(recon, first)
        workspace.add(request("character.read", categories=["stats"], refresh=True),
                      {"status": "success", "data": {"text": "b" * 4500}})
        self.assertEqual(workspace.select()[0][0]["result"]["status"], "not_in_context")
        self.assertTrue(workspace.activate(recon))
        self.assertEqual(workspace.select()[0][0]["result"]["data"]["observed_at"], "synthetic-time")

    def test_explicit_reactivation_beats_normal_tool_class_priority_for_next_turn(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        observed = request("state.read", categories=["vitals"])
        workspace.add(observed, {"status": "success", "data": {"text": "LIVE-OBSERVATION " + "a" * 4500}})
        workspace.add(request("knowledge.read", source_id="rules"), passage("rules", "b" * 4500))
        self.assertEqual(workspace.select()[0][0]["result"]["status"], "not_in_context")
        self.assertTrue(workspace.activate(observed))
        records, _ = workspace.select()
        self.assertEqual(records[0]["result"]["status"], "success")
        self.assertEqual(records[1]["result"]["status"], "not_in_context")

    def test_eight_oversize_provider_locators_cannot_exceed_minimum_context(self):
        workspace = EvidenceWorkspace(max_result_chars=3000, max_context_chars=5400, max_requests=8)
        for index in range(8):
            data = {field: "x" * 160 for field in ("source_id", "section_id", "start", "end", "next_cursor")}
            data["text"] = "x" * 5000
            workspace.add(request("knowledge.read", source_id=str(index)), {"status": "success", "data": data})
        records, sources = workspace.select()
        self.assertLessEqual(len(json.dumps(records, separators=(",", ":"))), 5400)
        self.assertEqual(sources, [])

    def test_read_identity_ignores_request_diagnostics_and_retrieval_clock_not_revision(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        first = passage("same", "ONE-PASSAGE", retrieved="original")
        again = passage("same", "ONE-PASSAGE", retrieved="later")
        again["diagnostics"] = [{"detail": "different search transport"}]
        workspace.add(request("knowledge.read", source_id="same"), first)
        workspace.add(request("knowledge.read", source_id="same", section_id="rules"), again)
        records, sources = workspace.select()
        self.assertEqual(len(records), 1)
        self.assertEqual(sources, first["sources"])
        workspace.add(request("knowledge.read", source_id="same", cursor="new-revision"),
                      passage("same", "ONE-PASSAGE", revision=2))
        self.assertEqual(len(workspace.select()[0]), 2)

    def test_overlapping_searches_deduplicate_candidates_not_response_envelope(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        workspace.add(request("knowledge.search", query="first"), discovery("shared", "first-only"))
        workspace.add(request("knowledge.search", query="second"), discovery("shared", "second-only"))
        records, sources = workspace.select()
        rendered = json.dumps(records)
        self.assertEqual(rendered.count("discovery-shared"), 1)
        self.assertIn("discovery-first-only", rendered)
        self.assertIn("discovery-second-only", rendered)
        self.assertEqual({source["source_id"] for source in sources}, {"shared", "first-only", "second-only"})

    def test_evicted_long_discovery_query_keeps_readable_source_handle(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        workspace.add(request("knowledge.search", query="topic " * 120), discovery("source-handle", padding=4200))
        workspace.add(request("knowledge.read", source_id="useful"), passage("useful", "x" * 4500))
        records, _ = workspace.select()
        manifest = records[0]
        self.assertEqual(manifest["result"]["status"], "not_in_context")
        self.assertEqual(manifest["result"]["data"]["source_ids"], ["source-handle"])
        self.assertNotIn("discovery-source-handle", json.dumps(manifest))

    def test_evicted_long_query_keeps_all_six_issued_handles(self):
        workspace = EvidenceWorkspace(max_result_chars=6000, max_context_chars=8400, max_requests=8)
        handles = ["source_" + str(index) * 16 for index in range(6)]
        workspace.add(request("knowledge.search", query="long-topic " * 100), discovery(*handles, padding=250))
        workspace.add(request("knowledge.read", source_id="selected"), passage("selected", "a" * 3300))
        workspace.add(request("knowledge.read", source_id="another"), passage("another", "b" * 2600))
        manifest = workspace.select()[0][0]
        self.assertEqual(manifest["result"]["status"], "not_in_context")
        self.assertEqual(manifest["result"]["data"]["source_ids"], handles)

    def test_minimum_context_bound_and_maximum_request_memory_bound(self):
        workspace = EvidenceWorkspace(max_result_chars=3000, max_context_chars=5400, max_requests=8)
        for index in range(8):
            workspace.add(request("knowledge.search", query=str(index) * 1500),
                          discovery(str(index), padding=500))
        records, _ = workspace.select()
        self.assertLessEqual(len(json.dumps(records, separators=(",", ":"))), 5400)
        self.assertEqual(len(workspace._requests), 8)
        self.assertLessEqual(sum(len(json.dumps(entry.record, separators=(",", ":")))
                                 for entry in workspace._entries), 8 * 3000)
        with self.assertRaises(ValueError):
            workspace.add(request("knowledge.search", query="ninth"), discovery("ninth"))

    def test_oversize_result_not_retained_as_fact_or_unbounded_metadata(self):
        workspace = EvidenceWorkspace(max_result_chars=3000, max_context_chars=5400, max_requests=8)
        oversized = passage("large", "NOT-RETAINED " * 3000)
        oversized["diagnostics"] = [{"detail": "TOO-LARGE-DIAGNOSTIC " * 3000}]
        workspace.add(request("knowledge.read", source_id="large"), oversized)
        records, sources = workspace.select()
        self.assertEqual(sources, [])
        self.assertNotIn("NOT-RETAINED", json.dumps(records))
        self.assertNotIn("TOO-LARGE-DIAGNOSTIC", json.dumps(workspace.diagnostics))
        self.assertEqual(records[0]["result"]["data"]["source_id"], "large")
        self.assertTrue(workspace.activate(request("knowledge.read", source_id="large")))


if __name__ == "__main__":
    unittest.main()
