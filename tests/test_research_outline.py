"""Synthetic discovery coverage contracts; no model, network, or game I/O."""

import json
from pathlib import Path
import tempfile
import unittest

from lich_agent_bridge.knowledge import KnowledgeBase


class ResearchOutlineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def discover(self, text, *, query="synthetic", max_chars=6000):
        (self.root / "rules.md").write_text(text, encoding="utf-8")
        session = KnowledgeBase(wiki_root=self.root).open_research(
            character="Example", max_chars=max_chars)
        self.addCleanup(session.close)
        result = session.search(query)
        self.assertEqual(len(result["data"]["items"]), 1)
        return session, result, result["data"]["items"][0]

    def test_outline_ranges_identify_nested_and_disjoint_reads_with_duplicate_titles(self):
        text = ("# Synthetic rules\nUnicode qualification: café.\n"
                "## Parent\nParent rule.\n### Details\nFIRST-CHILD.\n"
                "## Unrelated\nIndependent rule.\n### Details\nSECOND-CHILD.\n")
        session, _, candidate = self.discover(text)
        self.assertTrue(candidate["outline_complete"])
        for section in candidate["sections"]:
            with self.subTest(title=section["title"], section_id=section["section_id"]):
                self.assertIn("start", section,
                              "Discovery must expose coverage so a parent and child can be distinguished.")
                read = session.read(candidate["source_id"], section["section_id"])["data"]
                self.assertEqual((section["start"], section["end"]),
                                 (read["start"], read["end"]))
                self.assertEqual(read["text"], text[section["start"]:section["end"]])
                self.assertTrue(read["complete"])
        parent = next(s for s in candidate["sections"] if s["title"] == "Parent")
        unrelated = next(s for s in candidate["sections"] if s["title"] == "Unrelated")
        children = [s for s in candidate["sections"] if s["title"] == "Details"]
        self.assertEqual(len(children), 2)
        self.assertLess(parent["start"], children[0]["start"])
        self.assertEqual(parent["end"], children[0]["end"])
        self.assertEqual(parent["end"], unrelated["start"])
        self.assertGreaterEqual(children[1]["start"], unrelated["start"])

    def test_outline_coverage_is_full_section_even_when_read_needs_continuation(self):
        text = ("# Synthetic rules\n## Parent\n" + "Introductory rule. " * 200
                + "\n### Details\nLATE-CHILD.\n## Separate\nIndependent rule.\n")
        session, _, candidate = self.discover(text, max_chars=2500)
        parent = next(s for s in candidate["sections"] if s["title"] == "Parent")
        child = next(s for s in candidate["sections"] if s["title"] == "Details")
        self.assertIn("end", parent, "Discovery coverage must describe the full selected range.")
        page = session.read(candidate["source_id"], parent["section_id"])["data"]
        self.assertEqual(page["start"], parent["start"])
        self.assertLess(page["end"], parent["end"])
        self.assertGreater(child["start"], page["end"])
        self.assertFalse(page["complete"])
        passage = page["text"]
        while page["next_cursor"]:
            page = session.read(candidate["source_id"], parent["section_id"],
                                cursor=page["next_cursor"])["data"]
            passage += page["text"]
        self.assertEqual(page["end"], parent["end"])
        self.assertEqual(passage, text[parent["start"]:parent["end"]])
        self.assertIn("LATE-CHILD", passage)

    def test_truncated_outline_preserves_ranked_section_coverage_within_allowance(self):
        text = ("# Synthetic rules\n"
                + "".join(f"## Topic {i}\nUnrelated content.\n" for i in range(24))
                + "## Target rule\nTARGET-PASSAGE.\n")
        for allowance in (2200, 6000):
            with self.subTest(allowance=allowance):
                session, result, candidate = self.discover(
                    text, query="synthetic target rule", max_chars=allowance)
                self.assertFalse(candidate["outline_complete"])
                target = next(s for s in candidate["sections"] if s["title"] == "Target rule")
                self.assertIn("start", target)
                read = session.read(candidate["source_id"], target["section_id"])["data"]
                self.assertEqual((target["start"], target["end"]),
                                 (read["start"], read["end"]))
                self.assertIn("TARGET-PASSAGE", read["text"])
                packed = {"items": result["data"]["items"], "sources": result["sources"]}
                self.assertLessEqual(len(json.dumps(packed)), allowance)


if __name__ == "__main__":
    unittest.main()
