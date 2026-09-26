from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lich_agent_bridge.errors import ConfigurationError
from lich_agent_bridge.settings import (
    DecisionProviderKind,
    GeneralWebProvider,
    OnlineFallbackPolicy,
    ProviderKind,
    Settings,
)


class SettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.environment = {"HOME": str(self.home)}
        self.config = self.root / "config" / "config.toml"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_evidence_allowances_default_and_round_trip_per_profile(self) -> None:
        settings = Settings.load(self.config, environment=self.environment)
        self.assertEqual(settings.selected_profile.evidence_result_chars, 12_000)
        self.assertEqual(settings.selected_profile.evidence_total_chars, 36_000)
        updated = Settings.load(self.config, environment=self.environment, overrides={
            "profiles": {"default": {
                "evidence_result_chars": 16_000,
                "evidence_total_chars": 48_000,
            }},
        })
        updated.write()
        restored = Settings.load(self.config, environment=self.environment)
        self.assertEqual(restored.selected_profile.evidence_result_chars, 16_000)
        self.assertEqual(restored.selected_profile.evidence_total_chars, 48_000)
        self.assertEqual(restored.redacted()["profiles"]["default"]["evidence_total_chars"], 48_000)

    def test_evidence_allowances_reject_invalid_values_and_impossible_envelopes(self) -> None:
        cases = [
            {"evidence_result_chars": True},
            {"evidence_result_chars": 3000.5},
            {"evidence_result_chars": 0},
            {"evidence_result_chars": 100_001},
            {"evidence_total_chars": "36000"},
            {"evidence_total_chars": 300_001},
            {"evidence_result_chars": 12_000, "evidence_total_chars": 12_000},
        ]
        for values in cases:
            with self.subTest(values=values), self.assertRaisesRegex(ConfigurationError, "evidence_"):
                Settings.load(self.config, environment=self.environment, overrides={
                    "profiles": {"default": values},
                })

    def write_config(self, text: str) -> None:
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(text, encoding="utf-8")

    def test_defaults_are_immutable_secret_free_and_use_xdg_paths(self) -> None:
        state = self.root / "state"
        config = self.root / "xdg" / "lab.toml"
        settings = Settings.load(
            config,
            environment={
                "HOME": str(self.home),
                "XDG_STATE_HOME": str(state),
                "OPENAI_API_KEY": "must-not-be-retained",
            },
        )

        self.assertEqual(settings.path, config)
        self.assertEqual(settings.schema_version, 1)
        self.assertEqual(settings.server.host, "127.0.0.1")
        self.assertEqual(settings.server.port, 18_765)
        self.assertEqual(settings.server.mcp_port, 18_766)
        self.assertEqual(settings.selected_profile_name, "default")
        self.assertEqual(settings.selected_profile.provider, "codex")
        self.assertFalse(settings.selected_profile.web_search)
        self.assertEqual(settings.providers["codex"].kind, ProviderKind.CODEX)
        self.assertEqual(
            settings.providers["llama_cpp"].base_url,
            "http://127.0.0.1:8080/v1",
        )
        self.assertEqual(
            settings.knowledge.online_fallback,
            OnlineFallbackPolicy.WHEN_NEEDED,
        )
        self.assertEqual(
            settings.knowledge.general_web_provider, GeneralWebProvider.DISABLED
        )
        self.assertEqual(
            settings.storage.state_directory,
            state / "lich-agent-bridge",
        )
        self.assertEqual(
            settings.storage.action_token_file,
            state / "lich-agent-bridge" / "action-token",
        )
        self.assertEqual(
            settings.storage.timing_log,
            state / "lich-agent-bridge" / "timings.jsonl",
        )
        self.assertFalse(settings.decisions.enabled)
        self.assertIs(settings.decisions.kind, DecisionProviderKind.SYSTEM_ONE)
        self.assertEqual(settings.decisions.base_url, "https://api.typesafe.ai")
        self.assertEqual(settings.decisions.endpoint_path, "/v1/systemone")
        self.assertEqual(settings.decisions.credential_env, "JEV_API_KEY")
        self.assertEqual(settings.decisions.model, "jev-1.13.0")
        self.assertEqual(settings.decisions.timeout_seconds, 2.0)
        self.assertEqual(settings.decisions.player_interval_seconds, 1.0)
        self.assertEqual(settings.decisions.decision_ttl_seconds, 1.0)
        self.assertEqual(
            settings.decisions.audit_log,
            state / "lich-agent-bridge" / "decisions-shadow.jsonl",
        )
        with self.assertRaises(TypeError):
            settings.profiles["other"] = settings.selected_profile  # type: ignore[index]

    def test_toml_loads_named_profiles_and_openai_compatible_provider(self) -> None:
        self.write_config(
            """
schema_version = 1
selected_profile = "local"

[server]
host = "localhost"
port = 19000
mcp_port = 19001

[knowledge]
project_root = "../project"
wiki_root = "~/curated"
gswiki_database = "cache/wiki.sqlite3"
mirror_max_age_hours = 24
online_fallback = "disabled"

[storage]
state_directory = "state"
inventory_database = "state/inventory.sqlite3"
lich_data_directory = "~/lich-data"
game = "GSIV"
action_token_file = "state/token"
audit_log = "state/audit.jsonl"
controller_manifest = "controllers.json"

[providers.llama]
kind = "openai_compatible"
base_url = "http://127.0.0.1:8081/v1/"

[profiles.local]
provider = "llama"
model = "local-model"
reasoning_effort = "high"
timeout_seconds = 30
instructions_file = "instructions.md"
web_search = false
""".lstrip()
        )

        settings = Settings.load(self.config, environment=self.environment)

        self.assertEqual(settings.server.port, 19_000)
        self.assertEqual(settings.server.mcp_port, 19_001)
        self.assertEqual(settings.selected_profile.provider, "llama")
        self.assertEqual(settings.selected_profile.model, "local-model")
        self.assertEqual(settings.selected_profile.reasoning_effort, "high")
        self.assertEqual(
            settings.providers["llama"].base_url,
            "http://127.0.0.1:8081/v1",
        )
        self.assertEqual(
            settings.knowledge.project_root,
            self.root / "project",
        )
        self.assertEqual(settings.knowledge.wiki_root, self.home / "curated")
        self.assertEqual(
            settings.knowledge.gswiki_database,
            self.config.parent / "cache" / "wiki.sqlite3",
        )
        self.assertEqual(
            settings.selected_profile.instructions_file,
            self.config.parent / "instructions.md",
        )
        self.assertEqual(
            settings.storage.state_directory,
            self.config.parent / "state",
        )
        self.assertEqual(
            settings.storage.resolved_inventory_database,
            self.config.parent / "state" / "inventory.sqlite3",
        )

    def test_legacy_environment_overrides_toml(self) -> None:
        self.write_config(
            """
schema_version = 1
selected_profile = "default"

[server]
port = 19000

[profiles.default]
model = "from-file"
""".lstrip()
        )
        data_dir = self.root / "lich"
        environment = {
            **self.environment,
            "LAB_PORT": "19001",
            "LAB_MCP_PORT": "19002",
            "LAB_BACKEND": "openai",
            "LAB_MODEL": "gpt-env",
            "LAB_PROJECT_ROOT": str(self.root / "project-env"),
            "LAB_WIKI_ROOT": str(self.root / "wiki-env"),
            "LAB_GSWIKI_DB": str(self.root / "gswiki-env.sqlite3"),
            "LAB_LICH_DATA_DIR": str(data_dir),
            "LAB_GAME": "GSIV-test",
            "LAB_ACTION_TOKEN_FILE": str(self.root / "token-env"),
            "LAB_AUDIT_LOG": str(self.root / "audit-env"),
            "LAB_CONTROLLER_MANIFEST": str(self.root / "controllers-env.json"),
            "LAB_DECISIONS_ENABLED": "yes",
            "LAB_JEV_BASE_URL": "https://jev.example.test/api/",
            "LAB_DECISION_ENDPOINT_PATH": "/api/alpha/decisions",
            "LAB_JEV_MODEL": "jev-env",
            "LAB_JEV_CREDENTIAL_ENV": "PRIVATE_JEV_KEY",
            "LAB_JEV_TIMEOUT": "3.5",
            "LAB_DECISION_PLAYER_INTERVAL": "1.5",
            "LAB_DECISION_TTL": "2.5",
            "LAB_DECISION_AUDIT_LOG": str(self.root / "decisions-env.jsonl"),
        }

        settings = Settings.load(self.config, environment=environment)

        self.assertEqual(settings.server.port, 19_001)
        self.assertEqual(settings.server.mcp_port, 19_002)
        self.assertEqual(settings.selected_profile.provider, "openai")
        self.assertEqual(settings.selected_profile.model, "gpt-env")
        self.assertEqual(settings.knowledge.wiki_root, self.root / "wiki-env")
        self.assertEqual(
            settings.knowledge.gswiki_database, self.root / "gswiki-env.sqlite3"
        )
        self.assertEqual(settings.storage.lich_data_directory, data_dir)
        self.assertEqual(
            settings.storage.resolved_inventory_database,
            data_dir / "GSIV-test" / "lab-inventory.sqlite3",
        )
        self.assertEqual(settings.storage.action_token_file, self.root / "token-env")
        self.assertEqual(settings.storage.audit_log, self.root / "audit-env")
        self.assertEqual(
            settings.storage.controller_manifest,
            self.root / "controllers-env.json",
        )
        self.assertTrue(settings.decisions.enabled)
        self.assertEqual(settings.decisions.base_url, "https://jev.example.test/api")
        self.assertEqual(
            settings.decisions.endpoint_path, "/api/alpha/decisions"
        )
        self.assertEqual(settings.decisions.model, "jev-env")
        self.assertEqual(settings.decisions.credential_env, "PRIVATE_JEV_KEY")
        self.assertEqual(settings.decisions.timeout_seconds, 3.5)
        self.assertEqual(settings.decisions.player_interval_seconds, 1.5)
        self.assertEqual(settings.decisions.decision_ttl_seconds, 2.5)
        self.assertEqual(
            settings.decisions.audit_log, self.root / "decisions-env.jsonl"
        )

    def test_explicit_overrides_win_over_environment_and_file(self) -> None:
        self.write_config(
            """
schema_version = 1

[server]
port = 19000
""".lstrip()
        )

        settings = Settings.load(
            self.config,
            environment={**self.environment, "LAB_PORT": "19001"},
            overrides={
                "server": {"port": 19002},
                "profiles": {"default": {"reasoning_effort": "xhigh"}},
            },
        )

        self.assertEqual(settings.server.port, 19_002)
        self.assertEqual(settings.selected_profile.reasoning_effort, "xhigh")

    def test_local_system_one_http_and_no_credential_round_trip(self) -> None:
        settings = Settings.load(
            self.config,
            environment=self.environment,
            overrides={
                "decisions": {
                    "enabled": True,
                    "base_url": "http://[::1]:8000/",
                    "endpoint_path": "  /api/alpha/decisions  ",
                    "credential_env": None,
                }
            },
        )

        self.assertEqual(settings.decisions.base_url, "http://[::1]:8000")
        self.assertEqual(settings.decisions.endpoint_path, "/api/alpha/decisions")
        self.assertIsNone(settings.decisions.credential_env)
        settings.write()
        written = self.config.read_text(encoding="utf-8")
        self.assertIn('credential_env = ""', written)

        restored = Settings.load(self.config, environment=self.environment)
        self.assertTrue(restored.decisions.enabled)
        self.assertEqual(restored.decisions.endpoint_path, "/api/alpha/decisions")
        self.assertIsNone(restored.decisions.credential_env)

    def test_local_system_one_http_accepts_named_and_ipv4_loopback(self) -> None:
        for host in ("localhost", "127.0.0.1"):
            with self.subTest(host=host):
                settings = Settings.load(
                    self.config,
                    environment=self.environment,
                    overrides={
                        "decisions": {
                            "base_url": f"http://{host}:8000",
                            "credential_env": None,
                        }
                    },
                )
                self.assertEqual(
                    settings.decisions.base_url, f"http://{host}:8000"
                )

    def test_environment_can_select_credential_free_local_system_one(self) -> None:
        settings = Settings.load(
            self.config,
            environment={
                **self.environment,
                "LAB_DECISIONS_ENABLED": "true",
                "LAB_JEV_BASE_URL": "http://127.0.0.1:8000",
                "LAB_JEV_CREDENTIAL_ENV": "",
            },
        )

        self.assertTrue(settings.decisions.enabled)
        self.assertIsNone(settings.decisions.credential_env)

    def test_legacy_openai_backend_retains_its_default_model(self) -> None:
        settings = Settings.load(
            self.config,
            environment={**self.environment, "LAB_BACKEND": "openai"},
        )

        self.assertEqual(settings.selected_profile.provider, "openai")
        self.assertEqual(settings.selected_profile.model, "gpt-5.6")

    def test_legacy_lich_data_directory_beats_lower_precedence_database(self) -> None:
        self.write_config(
            """
schema_version = 1

[storage]
inventory_database = "configured.sqlite3"
""".lstrip()
        )
        data_dir = self.root / "lich"

        settings = Settings.load(
            self.config,
            environment={
                **self.environment,
                "LAB_LICH_DATA_DIR": str(data_dir),
                "LAB_GAME": "GSIV-test",
            },
        )

        self.assertIsNone(settings.storage.inventory_database)
        self.assertEqual(
            settings.storage.resolved_inventory_database,
            data_dir / "GSIV-test" / "lab-inventory.sqlite3",
        )

    def test_legacy_environment_paths_remain_relative_to_process_cwd(self) -> None:
        working = self.root / "working"
        working.mkdir()
        environment = {
            **self.environment,
            "LAB_WIKI_ROOT": "knowledge/wiki",
            "LAB_GSWIKI_DB": "cache/gswiki.sqlite3",
            "LAB_INVENTORY_DB": "state/inventory.sqlite3",
            "LAB_ACTION_TOKEN_FILE": "state/token",
            "LAB_INSTRUCTIONS_FILE": "agent/instructions.md",
            "LAB_CODEX_BIN": "bin/codex",
        }

        with mock.patch(
            "lich_agent_bridge.settings.Path.cwd", return_value=working
        ):
            settings = Settings.load(self.config, environment=environment)

        self.assertEqual(settings.knowledge.wiki_root, working / "knowledge/wiki")
        self.assertEqual(
            settings.knowledge.gswiki_database,
            working / "cache/gswiki.sqlite3",
        )
        self.assertEqual(
            settings.storage.resolved_inventory_database,
            working / "state/inventory.sqlite3",
        )
        self.assertEqual(
            settings.storage.action_token_file,
            working / "state/token",
        )
        self.assertEqual(
            settings.selected_profile.instructions_file,
            working / "agent/instructions.md",
        )
        self.assertEqual(
            settings.providers["codex"].command,
            str(working / "bin/codex"),
        )

    def test_action_and_audit_overrides_are_independent_of_xdg_state(self) -> None:
        state = self.root / "xdg-state"
        token = self.root / "private" / "token"
        audit = self.root / "logs" / "audit.jsonl"

        token_only = Settings.load(
            self.config,
            environment={
                **self.environment,
                "XDG_STATE_HOME": str(state),
                "LAB_ACTION_TOKEN_FILE": str(token),
            },
        )
        audit_only = Settings.load(
            self.config,
            environment={
                **self.environment,
                "XDG_STATE_HOME": str(state),
                "LAB_AUDIT_LOG": str(audit),
            },
        )

        expected_root = state / "lich-agent-bridge"
        self.assertEqual(token_only.storage.state_directory, expected_root)
        self.assertEqual(token_only.storage.action_token_file, token)
        self.assertEqual(
            token_only.storage.audit_log, expected_root / "actions.jsonl"
        )
        self.assertEqual(audit_only.storage.state_directory, expected_root)
        self.assertEqual(
            audit_only.storage.action_token_file, expected_root / "action-token"
        )
        self.assertEqual(audit_only.storage.audit_log, audit)
        self.assertEqual(
            audit_only.storage.timing_log, expected_root / "timings.jsonl"
        )

    def test_invalid_toml_has_path_and_parse_context(self) -> None:
        self.write_config("schema_version = [\n")

        with self.assertRaisesRegex(
            ConfigurationError, r"invalid TOML in settings file.*config\.toml"
        ):
            Settings.load(self.config, environment=self.environment)

    def test_unknown_keys_are_rejected_at_every_open_schema_level(self) -> None:
        cases = {
            "top": ("mystery = true\n", "unknown setting"),
            "server": ("[server]\nmystery = true\n", "unknown setting"),
            "decision mode": (
                "[decisions]\nmode = \"shadow\"\n", "unknown setting"
            ),
            "decision tick interval": (
                "[decisions]\ntick_interval_seconds = 2\n", "unknown setting"
            ),
            "provider": ((
                "[providers.local]\n"
                'kind = "openai_compatible"\n'
                'base_url = "http://127.0.0.1:8080/v1"\n'
                'api_key = "never-store-this"\n'
            ), "must not be stored"),
            "profile": ("[profiles.default]\nmystery = true\n", "unknown setting"),
        }
        for label, (addition, expected) in cases.items():
            with self.subTest(label=label):
                self.write_config(f"schema_version = 1\n{addition}")
                with self.assertRaisesRegex(ConfigurationError, expected):
                    Settings.load(self.config, environment=self.environment)

    def test_validation_errors_identify_the_setting_and_expected_value(self) -> None:
        cases = {
            "non-loopback host": (
                '[server]\nhost = "0.0.0.0"\n',
                "server.host must be localhost or a loopback IP address",
            ),
            "port": ("[server]\nport = 70000\n", "server.port must be between"),
            "MCP port": (
                "[server]\nmcp_port = 70000\n",
                "server.mcp_port must be between",
            ),
            "colliding ports": (
                "[server]\nport = 19000\nmcp_port = 19000\n",
                "server.mcp_port must differ",
            ),
            "profile reference": (
                'selected_profile = "missing"\n',
                "selected_profile references unknown profile",
            ),
            "provider URL credentials": (
                "[providers.bad]\n"
                'kind = "openai_compatible"\n'
                'base_url = "http://user:pass@localhost:8080/v1"\n',
                "must not contain credentials",
            ),
            "decision enabled": (
                '[decisions]\nenabled = "true"\n',
                "decisions.enabled must be true or false",
            ),
            "decision provider kind": (
                '[decisions]\nkind = "jev"\n',
                "decisions.kind must be one of: system_one",
            ),
            "decision HTTPS": (
                '[decisions]\nbase_url = "http://api.typesafe.ai"\n',
                "decisions.base_url must use HTTPS unless its HTTP host is loopback",
            ),
            "decision URL scheme": (
                '[decisions]\nbase_url = "ftp://localhost/systemone"\n',
                "decisions.base_url must be an absolute HTTP",
            ),
            "decision URL credentials": (
                '[decisions]\nbase_url = "https://user:pass@api.typesafe.ai"\n',
                "decisions.base_url must not contain credentials",
            ),
            "relative decision endpoint": (
                '[decisions]\nendpoint_path = "api/alpha/decisions"\n',
                "decisions.endpoint_path must be an absolute URL path",
            ),
            "decision endpoint scheme": (
                '[decisions]\nendpoint_path = "https://example.test/decisions"\n',
                "decisions.endpoint_path must be an absolute URL path",
            ),
            "decision endpoint authority": (
                '[decisions]\nendpoint_path = "//example.test/decisions"\n',
                "decisions.endpoint_path must be an absolute URL path",
            ),
            "decision endpoint query": (
                '[decisions]\nendpoint_path = "/v1/systemone?debug=true"\n',
                "decisions.endpoint_path must not contain a query or fragment",
            ),
            "decision endpoint fragment": (
                '[decisions]\nendpoint_path = "/v1/systemone#fragment"\n',
                "decisions.endpoint_path must not contain a query or fragment",
            ),
            "decision endpoint backslash": (
                "[decisions]\nendpoint_path = '/v1\\\\systemone'\n",
                "decisions.endpoint_path must not contain a backslash",
            ),
            "decision endpoint dot segment": (
                '[decisions]\nendpoint_path = "/v1/../systemone"\n',
                "decisions.endpoint_path must not contain dot segments",
            ),
            "decision endpoint whitespace": (
                '[decisions]\nendpoint_path = "/api/alpha decisions"\n',
                "decisions.endpoint_path must not contain whitespace or controls",
            ),
            "decision endpoint length": (
                '[decisions]\nendpoint_path = "/' + ("x" * 512) + '"\n',
                "decisions.endpoint_path must be at most 512 characters",
            ),
            "decision credential reference": (
                '[decisions]\ncredential_env = "not an env name"\n',
                "decisions.credential_env must be an environment variable name",
            ),
            "player interval": (
                "[decisions]\nplayer_interval_seconds = 0.1\n",
                "decisions.player_interval_seconds must be between",
            ),
        }
        for label, (addition, message) in cases.items():
            with self.subTest(label=label):
                self.write_config(f"schema_version = 1\n{addition}")
                with self.assertRaisesRegex(ConfigurationError, message):
                    Settings.load(self.config, environment=self.environment)

    def test_invalid_decision_enabled_environment_override_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ConfigurationError, "LAB_DECISIONS_ENABLED has an invalid value"
        ):
            Settings.load(
                self.config,
                environment={
                    **self.environment,
                    "LAB_DECISIONS_ENABLED": "sometimes",
                },
            )

    def test_default_path_honors_explicit_lab_config_xdg_and_home(self) -> None:
        explicit = self.root / "explicit.toml"
        lab_config = self.root / "from-env.toml"
        xdg = self.root / "xdg"

        self.assertEqual(
            Settings.path_for(explicit, environment=self.environment), explicit
        )
        self.assertEqual(
            Settings.path_for(
                environment={**self.environment, "LAB_CONFIG": str(lab_config)}
            ),
            lab_config,
        )
        self.assertEqual(
            Settings.path_for(
                environment={**self.environment, "XDG_CONFIG_HOME": str(xdg)}
            ),
            xdg / "lich-agent-bridge" / "config.toml",
        )
        self.assertEqual(
            Settings.path_for(environment=self.environment),
            self.home / ".config" / "lich-agent-bridge" / "config.toml",
        )

    def test_redacted_rendering_never_contains_credential_values(self) -> None:
        secret = "secret-value-that-must-not-leak"
        settings = Settings.load(
            self.config,
            environment={
                **self.environment,
                "OPENAI_API_KEY": secret,
                "JEV_API_KEY": secret,
            },
        )

        rendered = repr(settings.redacted())

        self.assertNotIn(secret, rendered)
        self.assertIn("OPENAI_API_KEY", rendered)
        self.assertIn("JEV_API_KEY", rendered)
        self.assertNotIn("api_key", settings.providers["openai"].__slots__)

    def test_atomic_write_round_trips_and_refuses_unconfirmed_replacement(self) -> None:
        settings = Settings.load(self.config, environment=self.environment)

        written = settings.write()
        original = written.read_bytes()
        round_tripped = Settings.load(written, environment=self.environment)

        self.assertEqual(written, self.config)
        self.assertEqual(round_tripped.to_mapping(), settings.to_mapping())
        written_text = written.read_text(encoding="utf-8")
        self.assertNotIn("action_token_file", written_text)
        self.assertNotIn("audit_log", written_text)
        self.assertEqual(oct(written.stat().st_mode & 0o777), "0o600")
        with self.assertRaisesRegex(ConfigurationError, "replace=True"):
            settings.write()
        self.assertEqual(written.read_bytes(), original)

    def test_failed_atomic_replacement_preserves_existing_file(self) -> None:
        self.write_config("original bytes that are not valid TOML\n")
        existing = self.config.read_bytes()
        settings = Settings.load(
            self.root / "missing.toml", environment=self.environment
        )

        with mock.patch(
            "lich_agent_bridge.settings.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                settings.write(self.config, replace=True)

        self.assertEqual(self.config.read_bytes(), existing)
        self.assertEqual(
            list(self.config.parent.glob(f".{self.config.name}.*")), []
        )


if __name__ == "__main__":
    unittest.main()
