import importlib.util
import json
from pathlib import Path
import unittest


def module():
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark-wiki-retrieval.py"
    spec = importlib.util.spec_from_file_location("wiki_retrieval_benchmark", path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


class WikiRetrievalBenchmarkTests(unittest.TestCase):
    def test_targets_use_served_order_and_shared_budget_not_gold_labels(self):
        benchmark = module()
        items = [{"source_id": "a", "sections": [
            {"section_id": "section", "kind": "section"},
            {"section_id": "passage1", "kind": "passage"},
            {"section_id": "passage2", "kind": "passage"}]},
            {"source_id": "b", "sections": [{"section_id": "other"}]}]
        self.assertEqual(list(benchmark.read_targets(items, 3)),
                         [("a", "passage1"), ("b", "other"), ("a", "passage2")])

    def test_benchmark_records_real_read_coverage_not_just_page_recall(self):
        benchmark = module()
        corpus = json.loads(benchmark.CORPUS.read_text())
        result = benchmark.benchmark(corpus, iterations=1)
        self.assertEqual(set(result["modes"]), {"legacy", "passage"})
        for mode in result["modes"].values():
            self.assertEqual(len(mode["cases"]), len(corpus["questions"]))
            self.assertTrue(all(case["reads"] <= 3 for case in mode["cases"]))
            self.assertTrue(0 <= mode["fact_recall"] <= 1)
        before = next(case for case in result["modes"]["legacy"]["cases"]
                      if case["id"] == "late-generic-heading")
        after = next(case for case in result["modes"]["passage"]["cases"]
                     if case["id"] == "late-generic-heading")
        self.assertEqual(before["sources_found"], 1)
        self.assertFalse(before["complete_facts"])
        self.assertTrue(after["complete_facts"])

    def test_invalid_run_envelope_is_rejected(self):
        benchmark = module()
        with self.assertRaises(ValueError):
            benchmark.benchmark({}, iterations=0)
