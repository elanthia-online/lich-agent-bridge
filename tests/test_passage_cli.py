import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from lich_agent_bridge import gswiki, labctl
from lich_agent_bridge.settings import Settings


class PassageCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "mirror.sqlite3"
        self.settings = Settings.load(
            path=self.root / "absent-config.toml", environment={},
            overrides={"knowledge": {"gswiki_database": str(self.database)}},
        )

    def mirror(self):
        with sqlite3.connect(self.database) as connection:
            gswiki._create_schema(connection)
            gswiki._upsert_page(connection, {
                "pageid": 1, "ns": 0, "title": "Synthetic Index Rule",
                "revisions": [{"revid": 42, "slots": {"main": {
                    "content": "== Rule ==\nA synthetic lamp illuminates a paper trail."
                }}}],
            }, "2026-01-01T00:00:00+00:00")
            connection.execute("INSERT INTO metadata VALUES ('last_sync', ?)",
                               ("2026-01-01T00:00:00+00:00",))

    def test_explicit_index_is_offline_and_preserves_mirror_freshness(self):
        self.mirror()
        with patch.object(labctl, "sync") as download, patch.object(labctl, "_token") as token:
            with patch.object(labctl.Settings, "load", return_value=self.settings):
                output = io.StringIO()
                with redirect_stdout(output):
                    labctl.main(["wiki", "index"])
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")
        download.assert_not_called()
        token.assert_not_called()
        summary = labctl._wiki_status(self.settings)
        self.assertEqual(summary["last_sync"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(summary["passage_index"]["status"], "ready")

    def test_legacy_status_never_implicitly_builds_index(self):
        self.mirror()
        before = self.database.read_bytes()
        summary = labctl._wiki_status(self.settings)
        self.assertEqual(summary["passage_index"]["status"], "missing")
        self.assertEqual(self.database.read_bytes(), before)

    def test_missing_mirror_exits_unsuccessfully_without_creating_it(self):
        with patch.object(labctl.Settings, "load", return_value=self.settings):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
                labctl.main(["wiki", "index"])
        self.assertEqual(error.exception.code, 1)
        self.assertFalse(self.database.exists())

    def test_failed_index_reports_retained_mirror(self):
        self.mirror()
        before = self.database.read_bytes()
        with patch("lich_agent_bridge.passage_index.rebuild", side_effect=OSError("synthetic failure")):
            result = labctl._wiki_index(self.settings)
        self.assertEqual(result["status"], "error")
        self.assertIn("previous mirror was retained", result["detail"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_wal_mirror_rejection_is_a_structured_cli_error(self):
        self.mirror()
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
        result = labctl._wiki_index(self.settings)
        self.assertEqual(result["status"], "error")
        self.assertIn("DELETE-journal", result["detail"])
        self.assertIn("previous mirror was retained", result["detail"])
