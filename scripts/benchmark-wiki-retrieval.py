#!/usr/bin/env python3
"""Compare legacy/page and passage retrieval on independent synthetic evidence.

No model, network, game session, or installed mirror is used. This deterministic
reader is a retrieval probe, not a simulation of an intelligent answer model.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lich_agent_bridge.gswiki import _create_schema, _upsert_page
from lich_agent_bridge.knowledge import KnowledgeBase
from lich_agent_bridge.passage_index import build_index


CORPUS = Path(__file__).resolve().parents[1] / "tests/fixtures/wiki_retrieval/corpus.json"
STAMP = "2026-01-01T00:00:00+00:00"


def create_mirror(database: Path, corpus: dict) -> None:
    with sqlite3.connect(database) as connection:
        _create_schema(connection)
        for page_id, page in enumerate(corpus["pages"], 1):
            text = page["text"]
            count = page.get("padding_paragraphs", 0)
            if count:
                heading, separator, body = text.partition("\n")
                padding = ("Historical commentary describes unrelated maintenance records. "
                           "Additional background remains available in this archive.\n\n") * count
                text = heading + separator + padding + body
            _upsert_page(connection, {
                "pageid": page_id, "ns": 0, "title": page["title"],
                "revisions": [{"revid": 100 + page_id, "timestamp": STAMP,
                               "slots": {"main": {"content": text}}}],
            }, STAMP)
        connection.execute("INSERT INTO metadata VALUES ('last_sync', ?)", (STAMP,))


def read_targets(items: list[dict], budget: int):
    """Round-robin served candidates/handles without consulting expected facts."""
    choices = []
    for item in items:
        sections = item.get("sections", [])
        passages = [section for section in sections if section.get("kind") == "passage"]
        choices.append([(item["source_id"], section["section_id"])
                        for section in (passages or sections)] or [(item["source_id"], None)])
    issued = 0
    for depth in range(max(map(len, choices), default=0)):
        for targets in choices:
            if depth < len(targets):
                if issued >= budget:
                    return
                yield targets[depth]
                issued += 1


def run_question(knowledge: KnowledgeBase, question: dict, read_budget: int) -> dict:
    session = knowledge.open_research(character="Example", max_chars=6000)
    try:
        start = time.perf_counter()
        result = session.search(question["query"])
        search_ms = (time.perf_counter() - start) * 1000
        items = result["data"]["items"]
        readings = []
        read_ms = 0.0
        for source_id, section_id in read_targets(items, read_budget):
            start = time.perf_counter()
            reading = session.read(source_id, section_id)
            read_ms += (time.perf_counter() - start) * 1000
            if reading["status"] == "succeeded":
                readings.append(reading["data"]["text"])
        # Score only text actually returned by read; discovery and full stored
        # documents do not count as supplied evidence.
        text = "\n".join(readings).casefold()
        facts = [fact.casefold() in text for fact in question["facts"]]
        titles = {item["title"] for item in items}
        return {"id": question["id"], "search_ms": search_ms, "read_ms": read_ms,
                "total_ms": search_ms + read_ms, "reads": len(readings),
                "retrieved_chars": sum(map(len, readings)),
                "facts_found": sum(facts), "facts_total": len(facts),
                "complete_facts": all(facts),
                "sources_found": sum(title in titles for title in question["sources"]),
                "sources_total": len(question["sources"]),
                "challenge": question.get("challenge"),
                "discovery_status": result["status"]}
    finally:
        session.close()


def distribution(values: list[float]) -> dict:
    values = sorted(values)
    return {"p50": round(statistics.median(values), 3),
            "p95": round(values[max(0, math.ceil(len(values) * 0.95) - 1)], 3)}


def benchmark(corpus: dict, *, iterations: int = 5, read_budget: int = 3) -> dict:
    if not 1 <= iterations <= 100 or not 1 <= read_budget <= 8:
        raise ValueError("iterations must be 1..100 and read budget 1..8")
    report = {"corpus_pages": len(corpus["pages"]), "questions": len(corpus["questions"]),
              "iterations": iterations, "read_budget": read_budget,
              "limitations": ["Synthetic warmed local timings, not full-wiki or cold-cache measurements.",
                              "Fixed round-robin reader, not model selection or answer accuracy.",
                              "No semantic search; paraphrase challenge remains in the corpus."],
              "modes": {}}
    with tempfile.TemporaryDirectory(prefix="lab-retrieval-benchmark-") as temporary:
        root = Path(temporary)
        wiki = root / "empty-curated"
        wiki.mkdir()
        for mode in ("legacy", "passage"):
            database = root / f"{mode}.sqlite3"
            create_mirror(database, corpus)
            if mode == "passage":
                start = time.perf_counter()
                with sqlite3.connect(database) as connection:
                    index_report = build_index(connection)
                report["index"] = {"elapsed_ms": (time.perf_counter() - start) * 1000,
                                   "coverage": index_report}
            knowledge = KnowledgeBase(wiki_root=wiki, gswiki_database=database,
                                      now=lambda: datetime(2026, 1, 1, tzinfo=UTC))
            # Discard one warmup per question; new question handles each time.
            for question in corpus["questions"]:
                run_question(knowledge, question, read_budget)
            runs = [run_question(knowledge, question, read_budget)
                    for _ in range(iterations) for question in corpus["questions"]]
            required = [run for run in runs if not run["challenge"]]
            report["modes"][mode] = {
                "database_bytes": database.stat().st_size,
                "search_ms": distribution([run["search_ms"] for run in runs]),
                "read_ms": distribution([run["read_ms"] for run in runs]),
                "total_ms": distribution([run["total_ms"] for run in runs]),
                "fact_recall": sum(run["facts_found"] for run in runs) / sum(run["facts_total"] for run in runs),
                "required_fact_recall": sum(run["facts_found"] for run in required) / sum(run["facts_total"] for run in required),
                "source_recall": sum(run["sources_found"] for run in runs) / sum(run["sources_total"] for run in runs),
                "cases": [{**runs[index], "search_ms": round(runs[index]["search_ms"], 3),
                           "read_ms": round(runs[index]["read_ms"], 3),
                           "total_ms": round(runs[index]["total_ms"], 3)}
                          for index in range(len(corpus["questions"]))],
            }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--read-budget", type=int, default=3)
    args = parser.parse_args()
    print(json.dumps(benchmark(json.loads(CORPUS.read_text()), iterations=args.iterations,
                               read_budget=args.read_budget), indent=2))


if __name__ == "__main__":
    main()
