import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import ANY, Mock, call, patch
from urllib.error import HTTPError

from lich_agent_bridge import labctl
from lich_agent_bridge.settings import Settings


class LabctlTests(unittest.TestCase):
    def test_config_path_uses_isolated_xdg_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "HOME": temporary,
                "XDG_CONFIG_HOME": str(Path(temporary) / "configuration"),
            }
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=True):
                with redirect_stdout(output):
                    labctl.main(["config", "path"])

        self.assertEqual(
            output.getvalue().strip(),
            str(
                Path(temporary)
                / "configuration"
                / "lich-agent-bridge"
                / "config.toml"
            ),
        )

    def test_setup_writes_valid_config_under_isolated_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = {
                "HOME": temporary,
                "XDG_CONFIG_HOME": str(Path(temporary) / "configuration"),
                "XDG_STATE_HOME": str(Path(temporary) / "state"),
            }
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=True):
                with patch("builtins.input", return_value=""):
                    with redirect_stdout(output):
                        labctl.main(["setup"])
                config = Settings.path_for()
                resolved = Settings.load()

            self.assertTrue(config.is_file())
            self.assertEqual(resolved.path, config)
            self.assertIn("Wrote", output.getvalue())
            self.assertFalse((Path(temporary) / "unrelated").exists())

    def test_setup_can_edit_one_section(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            with patch.dict(os.environ, {"HOME": temporary}, clear=True):
                with patch(
                    "builtins.input", side_effect=["server", "", "19001", ""]
                ):
                    labctl.main(["--config", str(config), "setup"])
                resolved = Settings.load(path=config)

        self.assertEqual(resolved.server.host, "127.0.0.1")
        self.assertEqual(resolved.server.port, 19001)
        self.assertEqual(resolved.server.mcp_port, 18766)

    def test_setup_agent_section_accepts_all_nullable_defaults(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            prompts = []
            answers = iter(["agent", "", "", "", "", "", "", ""])

            def answer(prompt):
                prompts.append(prompt)
                return next(answers)

            with patch.dict(os.environ, {"HOME": temporary}, clear=True):
                with patch("builtins.input", side_effect=answer):
                    labctl.main(["--config", str(config), "setup"])
                resolved = Settings.load(path=config)

        self.assertEqual(resolved.selected_profile.provider, "codex")
        self.assertIsNone(resolved.selected_profile.model)
        self.assertIsNone(resolved.selected_profile.reasoning_effort)
        self.assertIsNone(resolved.selected_profile.instructions_file)
        self.assertFalse(any("credential value" in prompt.casefold() for prompt in prompts))

    def test_setup_storage_section_accepts_unset_optional_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            answers = iter(["storage", "", "", "", "", "", ""])

            with (
                patch("builtins.input", side_effect=lambda _prompt: next(answers)),
                patch("builtins.print"),
            ):
                labctl.main(["--config", str(config), "setup"])

            resolved = Settings.load(path=config, environment={})
            self.assertIsNone(resolved.storage.inventory_database)
            self.assertIsNone(resolved.storage.lich_data_directory)
            self.assertEqual(
                resolved.storage.controller_manifest,
                resolved.knowledge.project_root / "lich" / "lab-controllers.json",
            )

    def test_setup_preserves_existing_file_without_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            original = b"this is not toml = [\n"
            config.write_bytes(original)
            output = io.StringIO()
            with patch.dict(os.environ, {"HOME": temporary}, clear=True):
                with patch("builtins.input", return_value="n"):
                    with redirect_stdout(output):
                        labctl.main(["--config", str(config), "setup"])

            self.assertEqual(config.read_bytes(), original)
            self.assertIn("Preserved", output.getvalue())

    def test_setup_can_replace_an_invalid_file_after_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text("schema_version = [\n", encoding="utf-8")
            answers = iter(["yes", ""])

            with patch.dict(os.environ, {"HOME": temporary}, clear=True):
                with patch(
                    "builtins.input", side_effect=lambda _prompt: next(answers)
                ):
                    with patch("builtins.print"):
                        labctl.main(["--config", str(config), "setup"])
                resolved = Settings.load(path=config)

        self.assertEqual(resolved.schema_version, 1)
        self.assertEqual(resolved.path, config)

    def test_setup_preserves_existing_valid_file_without_confirmation(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            with patch.dict(os.environ, {"HOME": temporary}, clear=True):
                Settings.load(path=config).write()
                original = config.read_bytes()
                with patch("builtins.input", return_value=""):
                    labctl.main(["--config", str(config), "setup"])

            self.assertEqual(config.read_bytes(), original)

    def test_config_show_never_prints_credential_value(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            environment = {
                "HOME": temporary,
                "OPENAI_API_KEY": "do-not-print-this-secret",
            }
            with patch.dict(os.environ, environment, clear=True):
                Settings.load(path=config).write()
                output = io.StringIO()
                with redirect_stdout(output):
                    labctl.main(["--config", str(config), "config", "show"])

        rendered = output.getvalue()
        self.assertNotIn("do-not-print-this-secret", rendered)
        self.assertEqual(json.loads(rendered)["schema_version"], 1)

    def test_doctor_is_local_read_only_and_reports_missing_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            environment = {"HOME": temporary}
            with patch.dict(os.environ, environment, clear=True):
                Settings.load(path=config).write()
                output = io.StringIO()
                with patch.object(
                    labctl,
                    "_service_check",
                    return_value=labctl._check(
                        "session_hub", "warning", "not ready on loopback"
                    ),
                ) as service:
                    with patch.object(labctl, "_token") as token:
                        with patch.object(labctl, "_post") as post:
                            with redirect_stdout(output):
                                labctl.main(
                                    ["--config", str(config), "doctor"]
                                )

        report = json.loads(output.getvalue())
        token.assert_not_called()
        post.assert_not_called()
        service.assert_has_calls(
            [
                call("127.0.0.1", 18765, name="session_hub"),
                call("127.0.0.1", 18766, name="mcp_adapter"),
            ]
        )
        self.assertEqual(report["status"], "warning")
        self.assertTrue(
            any(check["name"] == "session_hub" for check in report["checks"])
        )

    def test_gswiki_check_reports_healthy_recent_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "gswiki.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE pages (page_id INTEGER PRIMARY KEY);
                CREATE VIRTUAL TABLE pages_fts USING fts5(title, plain_text);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value)
                VALUES ('last_sync', '2999-01-01T00:00:00+00:00');
                """
            )
            connection.close()

            result = labctl._gswiki_check(str(database), 168.0)

        self.assertEqual(result["status"], "ok")

    def test_wiki_status_reports_path_size_last_sync_and_freshness(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "gswiki.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE pages (page_id INTEGER PRIMARY KEY);
                CREATE VIRTUAL TABLE pages_fts USING fts5(title, plain_text);
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO metadata(key, value)
                VALUES ('last_sync', '2999-01-01T00:00:00+00:00');
                """
            )
            connection.close()
            settings = Mock()
            settings.knowledge.gswiki_database = database
            settings.knowledge.mirror_max_age_hours = 168.0

            result = labctl._wiki_status(settings)

        self.assertEqual(result["path"], str(database))
        self.assertGreater(result["size_bytes"], 0)
        self.assertEqual(result["last_sync"], "2999-01-01T00:00:00+00:00")
        self.assertEqual(result["health"]["status"], "ok")

    def test_wiki_refresh_reports_retained_previous_mirror_on_failure(self):
        settings = Mock()
        settings.knowledge.gswiki_database = Path("/tmp/gswiki.sqlite3")
        with patch.object(labctl, "sync", side_effect=OSError("offline")):
            result = labctl._wiki_refresh(settings, namespaces=[0], delay=0)

        self.assertEqual(result["status"], "error")
        self.assertIn("previous mirror was retained", result["detail"])

    def test_doctor_refuses_non_loopback_service_probe(self):
        with patch.object(labctl, "urlopen") as opener:
            result = labctl._service_check("example.com", 18765)

        opener.assert_not_called()
        self.assertEqual(result["status"], "error")

    def test_backend_check_probes_selected_loopback_compatible_endpoint(self):
        values = {
            "selected_profile": "local",
            "profiles": {"local": {"provider": "llama_cpp"}},
            "providers": {
                "llama_cpp": {
                    "kind": "openai_compatible",
                    "command": None,
                    "base_url": "http://127.0.0.1:8080/v1",
                    "credential_env": None,
                }
            },
        }
        with patch.object(labctl, "urlopen", side_effect=OSError("offline")) as opener:
            result = labctl._backend_check(values, {}, probe=True)

        self.assertEqual(result["status"], "warning")
        self.assertEqual(opener.call_args.args[0].full_url, "http://127.0.0.1:8080/v1/models")

    def test_backend_check_probes_direct_openai_without_exposing_credential(self):
        credential = "private-test-credential"
        values = {
            "selected_profile": "cloud",
            "profiles": {"cloud": {"provider": "openai"}},
            "providers": {
                "openai": {
                    "kind": "openai",
                    "command": None,
                    "base_url": "https://api.openai.com/v1",
                    "credential_env": "TEST_OPENAI_KEY",
                }
            },
        }
        with patch.object(
            labctl,
            "urlopen",
            return_value=io.BytesIO(b'{"object":"list","data":[]}'),
        ) as opener:
            result = labctl._backend_check(
                values, {"TEST_OPENAI_KEY": credential}, probe=True
            )

        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.openai.com/v1/models")
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(credential, repr(result))

    def test_backend_check_reports_authentication_as_unverified(self):
        credential = "private-test-credential"
        values = {
            "selected_profile": "cloud",
            "profiles": {"cloud": {"provider": "openai"}},
            "providers": {
                "openai": {
                    "kind": "openai",
                    "command": None,
                    "base_url": "https://api.openai.com/v1",
                    "credential_env": "TEST_OPENAI_KEY",
                }
            },
        }
        unauthorized = HTTPError(
            "https://api.openai.com/v1/models", 401, "Unauthorized", {}, None
        )
        with patch.object(labctl, "urlopen", side_effect=unauthorized):
            result = labctl._backend_check(
                values, {"TEST_OPENAI_KEY": credential}, probe=True
            )

        self.assertEqual(result["status"], "warning")
        self.assertIn("authentication not probed", result["detail"])
        self.assertNotIn(credential, repr(result))

    def test_service_check_reports_malformed_health_json_as_warning(self):
        with patch.object(
            labctl,
            "urlopen",
            return_value=io.BytesIO(b'["not", "a", "health", "object"]'),
        ):
            result = labctl._service_check("127.0.0.1", 18765)

        self.assertEqual(result["status"], "warning")
        self.assertIn("invalid", result["detail"])

    def test_timings_reads_local_report_without_sidecar_or_token(self):
        output = io.StringIO()
        report = {"metrics": {"snapshot.age_ms": {"count": 2}}}
        with patch.object(labctl, "timing_report", return_value=report) as read_report:
            with patch.object(labctl, "_token") as token:
                with redirect_stdout(output):
                    labctl.main(["timings"])

        read_report.assert_called_once_with(
            timings_path=ANY,
            audit_path=ANY,
        )
        token.assert_not_called()
        self.assertIn('"snapshot.age_ms"', output.getvalue())

    def test_status_uses_public_health(self):
        output = io.StringIO()
        with patch.object(labctl, "_request", return_value={"status": "ok"}) as request:
            with redirect_stdout(output):
                labctl.main(["status"])
        request.assert_called_once_with("/health", settings=ANY)
        self.assertIn('"status": "ok"', output.getvalue())

    def test_state_uses_authenticated_url_encoded_character(self):
        output = io.StringIO()
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(
                labctl, "_request", return_value={"snapshot": {}}
            ) as request:
                with redirect_stdout(output):
                    labctl.main(["state", "Test Name"])
        request.assert_called_once_with(
            "/v1/state/Test%20Name", settings=ANY, token="secret"
        )

    def test_watch_once_prints_events_as_json_lines(self):
        page = {
            "cursor": 4,
            "events": [{"cursor": 4, "kind": "threat"}],
            "timed_out": False,
        }
        output = io.StringIO()
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(labctl, "_request", return_value=page) as request:
                with redirect_stdout(output):
                    labctl.main(
                        ["watch", "Testscout", "--cursor", "3", "--timeout", "0", "--once"]
                    )
        self.assertIn('"kind":"threat"', output.getvalue())
        self.assertEqual(request.call_args.kwargs["token"], "secret")

    def test_inventory_uses_compact_session_hub_route(self):
        output = io.StringIO()
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(
                labctl, "_post", return_value={"items": [], "total": 0}
            ) as request:
                with redirect_stdout(output):
                    labctl.main(["inventory", "Testmage", "black crystal"])
        request.assert_called_once_with(
            "/v1/session/inventory/find",
            {"character": "Testmage", "query": "black crystal"},
            settings=ANY,
            token="secret",
        )

    def test_combat_report_uses_authenticated_read_route(self):
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(labctl, "_post", return_value={"status": "unavailable"}) as request:
                with redirect_stdout(io.StringIO()):
                    labctl.main(["combat-report", "Testmage", "--operation-id", "a" * 16])
        request.assert_called_once_with("/v1/session/combat/report",
                                       {"character": "Testmage", "operation_id": "a" * 16},
                                       settings=ANY, token="secret")

    def test_sources_uses_authenticated_last_answer_provenance_route(self):
        output = io.StringIO()
        response = {"character": "Testmage", "answer_available": False, "sources": []}
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(labctl, "_post", return_value=response) as request:
                with redirect_stdout(output):
                    labctl.main(["sources", "Testmage"])

        request.assert_called_once_with(
            "/v1/session/sources",
            {"character": "Testmage"},
            settings=ANY,
            token="secret",
        )
        self.assertIn('"answer_available": false', output.getvalue())

    def test_perform_wait_streams_progress_and_terminal_record(self):
        output = io.StringIO()
        responses = [
            {"operation_id": "op-1", "status": "requested"},
            {
                "items": [{"cursor": 2, "status": "running", "detail": "ready"}],
                "cursor": "2",
                "operation": {"operation_id": "op-1", "status": "succeeded"},
            },
        ]
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(labctl, "_post", side_effect=responses) as request:
                with redirect_stdout(output):
                    labctl.main(
                        [
                            "perform",
                            "Testmage",
                            "item.audit",
                            "--item-id",
                            "100",
                            "--method",
                            "405",
                            "--wait",
                            "--timeout",
                            "0",
                        ]
                    )
        self.assertEqual(request.call_count, 2)
        self.assertIn('"detail":"ready"', output.getvalue())
        self.assertIn('"status": "succeeded"', output.getvalue())

    def test_stop_interrupts_by_character(self):
        with patch.object(labctl, "_token", return_value="secret"):
            with patch.object(labctl, "_post", return_value={"stopped": True}) as request:
                labctl.main(["stop", "Testscout"])
        request.assert_called_once_with(
            "/v1/session/operation/stop",
            {"character": "Testscout"},
            settings=ANY,
            token="secret",
        )


if __name__ == "__main__":
    unittest.main()
