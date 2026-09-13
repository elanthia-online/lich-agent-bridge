"""Loopback-only HTTP adapter for the Lich bridge."""

from __future__ import annotations

import argparse
import json
import hmac
import os
import stat
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, unquote, urlsplit

from .actions import (
    ActionApproval,
    ActionBroker,
    ActionContext,
    ActionControl,
    ActionNextRequest,
    ActionProposal,
    ActionResult,
    ActionStatusRequest,
    CommandPolicy,
    JsonlAuditLog,
    load_or_create_action_token,
)
from .codex_model import CodexExecModel
from .context_assembler import ContextAssembler
from .controller_manifest import ControllerManifest
from .engine import Copilot, Model
from .errors import (ConfigurationError, ModelError, ValidationError, QuestionBusy,
                     QuestionCapacity, QuestionInvalidated, QuestionTimeout)
from .inventory import InventoryKnowledge
from .character_knowledge import CharacterKnowledge
from .evidence_tools import EvidenceTools
from .evidence_loop import EvidenceLoop
from .knowledge import KnowledgeBase
from .model import OpenAICompatibleChatModel, OpenAIResponsesModel
from .protocol import (
    AskRequest,
    CharacterRequest,
    CharacterSnapshot,
    MeaningfulEvent,
    Observation,
)
from .session_hub import SessionHub
from .settings import ProviderKind, Settings
from .timings import TimingRecorder
from .watchers import WatcherReducer
from .world_state import MAX_WATCH_TIMEOUT_SECONDS, StateConflict, WorldState

MAX_REQUEST_BYTES = 1_048_576
MAX_CUSTOM_INSTRUCTIONS_BYTES = 16_384


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 18765

    @classmethod
    def from_settings(cls, settings: Settings) -> "ServerConfig":
        return cls(host=settings.server.host, port=settings.server.port)

    @classmethod
    def from_environment(cls) -> "ServerConfig":
        """Compatibility wrapper around the central settings module."""

        return cls.from_settings(Settings.load())


class LabHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        copilot: Copilot,
        actions: ActionBroker,
        action_token: str,
        world_state: WorldState,
        session_hub: SessionHub,
        watchers: WatcherReducer,
        character_knowledge: CharacterKnowledge | None = None,
    ):
        self.copilot = copilot
        self.actions = actions
        self.action_token = action_token
        self.world_state = world_state
        self.session_hub = session_hub
        self.watchers = watchers
        self.character_knowledge = character_knowledge
        self.character_database_status = "not_attempted"
        self._state_admission_lock = Lock()
        super().__init__(address, LabRequestHandler)

    def publish_snapshot(self, snapshot: CharacterSnapshot) -> dict[str, Any]:
        # State publication and dependent fences must have the same order when
        # two bridge generations overlap briefly during a reconnect.
        with self._state_admission_lock:
            result = self.copilot.admit_generation(
                snapshot.character, snapshot.generation,
                publish=lambda: self.world_state.publish_snapshot(snapshot),
            )
            self.actions.admit_generation(snapshot.character, snapshot.generation)
            if self.character_knowledge is not None:
                stored = self.character_knowledge.record(snapshot)
                self.character_database_status = stored["status"]
            return result


class LabRequestHandler(BaseHTTPRequestHandler):
    server: LabHTTPServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        try:
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._json(
                    200,
                    {
                        "service": "lich-agent-bridge",
                        "version": "0.2.0",
                        "status": "ok",
                        "capability": "confirmed_actions",
                        "backend": self.server.copilot.model_backend,
                        "model_configured": self.server.copilot.model_configured,
                        "ask_timeout_seconds": self.server.copilot.question_timeout_seconds,
                        "character_database_status": self.server.character_database_status,
                    },
                )
                return
            if not parsed.path.startswith("/v1/"):
                self._json(404, {"error": "not_found"})
                return
            if not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            if parsed.path.startswith("/v1/state/"):
                self._reject_query(parsed.query)
                character = self._path_character(parsed.path, "/v1/state/")
                result = self.server.world_state.snapshot(character)
                if result is None:
                    self._json(404, {"error": "state_not_found"})
                else:
                    self._json(200, result)
                return
            if parsed.path.startswith("/v1/watch/"):
                character = self._path_character(parsed.path, "/v1/watch/")
                cursor, timeout = self._watch_query(parsed.query)
                self._json(
                    200,
                    self.server.world_state.watch(
                        character, cursor=cursor, timeout=timeout
                    ),
                )
                return
            self._json(404, {"error": "not_found"})
        except ValidationError as error:
            self._json(400, {"error": "invalid_request", "detail": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._json(500, {"error": "internal_error"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler interface
        try:
            parsed = urlsplit(self.path)
            if parsed.query:
                raise ValidationError("POST routes do not accept query parameters")
            if parsed.path.startswith("/v1/") and not self._authorized():
                self._json(401, {"error": "unauthorized"})
                return
            payload = self._read_json()
            if parsed.path == "/v1/observe":
                events = payload.get("events") if isinstance(payload, dict) else None
                if not isinstance(events, list):
                    raise ValidationError("events must be an array")
                observations = [Observation.from_mapping(event) for event in events]
                accepted = self.server.copilot.observe(observations)
                self._json(202, {"accepted": accepted, "capability": "read_only"})
            elif parsed.path == "/v1/ask":
                answer = self.server.copilot.ask(AskRequest.from_mapping(payload))
                self._json(200, answer.to_mapping())
            elif parsed.path == "/v1/session/sources":
                request = CharacterRequest.from_mapping(payload)
                self._json(200, self.server.copilot.sources(request.character))
            elif parsed.path == "/v1/session/context":
                request = CharacterRequest.from_mapping(payload)
                self._json(200, self.server.copilot.context(request.character))
            elif parsed.path == "/v1/session/forget":
                request = CharacterRequest.from_mapping(payload)
                self._json(200, self.server.copilot.forget(request.character))
            elif parsed.path == "/v1/state":
                snapshot = CharacterSnapshot.from_mapping(payload)
                result = self.server.publish_snapshot(snapshot)
                if self.server.watchers.supports(snapshot.character):
                    for alert in self.server.watchers.reduce(snapshot):
                        self.server.world_state.publish_event(
                            MeaningfulEvent(
                                character=snapshot.character,
                                generation=snapshot.generation,
                                observed_at=snapshot.observed_at,
                                kind="watcher_alert",
                                summary=f"{alert.severity} {alert.status}: {alert.code}",
                                data=alert.to_mapping(),
                            )
                        )
                self._json(202, result)
            elif parsed.path == "/v1/event":
                result = self.server.world_state.publish_event(
                    MeaningfulEvent.from_mapping(payload)
                )
                self._json(202, result)
            elif parsed.path == "/v1/session/snapshot":
                self._json(200, self.server.session_hub.snapshot(payload))
            elif parsed.path == "/v1/session/watch":
                self._json(200, self.server.session_hub.watch(payload))
            elif parsed.path == "/v1/session/combat/report":
                self._json(200, self.server.session_hub.combat_report(payload))
            elif parsed.path == "/v1/session/inventory/find":
                self._json(200, self.server.session_hub.inventory_find(payload))
            elif parsed.path == "/v1/session/wiki/search":
                self._json(200, self.server.session_hub.wiki_search(payload))
            elif parsed.path == "/v1/session/alerts":
                self._json(200, self.server.session_hub.alerts(payload))
            elif parsed.path == "/v1/session/capabilities":
                self._json(200, self.server.session_hub.capability_catalog(payload))
            elif parsed.path == "/v1/session/perform":
                self._json(202, self.server.session_hub.start_operation(payload))
            elif parsed.path == "/v1/session/operation/watch":
                self._json(200, self.server.session_hub.watch_operation(payload))
            elif parsed.path == "/v1/session/operation/stop":
                self._json(200, self.server.session_hub.stop_operation(payload))
            elif parsed.path == "/v1/session/operation/control":
                self._json(202, self.server.session_hub.control_operation(payload))
            elif parsed.path.startswith("/v1/actions/"):
                if parsed.path == "/v1/actions/control":
                    result = self.server.actions.control(ActionControl.from_mapping(payload))
                    self._json(200, result)
                elif parsed.path == "/v1/actions/propose":
                    result = self.server.actions.submit(ActionProposal.from_mapping(payload))
                    self._json(201, result)
                elif parsed.path == "/v1/actions/poll":
                    result = self.server.actions.poll(ActionContext.from_mapping(payload))
                    self._json(200, {"action": result})
                elif parsed.path == "/v1/actions/next":
                    request = ActionNextRequest.from_mapping(payload)
                    result = self.server.actions.poll_wait(
                        request.context,
                        timeout_seconds=request.timeout_seconds,
                    )
                    self._json(200, {"action": result})
                elif parsed.path == "/v1/actions/approve":
                    result = self.server.actions.approve(ActionApproval.from_mapping(payload))
                    self._json(200, result)
                elif parsed.path == "/v1/actions/result":
                    result = self.server.actions.record_result(ActionResult.from_mapping(payload))
                    self._json(200, result)
                elif parsed.path == "/v1/actions/status":
                    request = ActionStatusRequest.from_mapping(payload)
                    self._json(200, self.server.actions.get(request.action_id))
                else:
                    self._json(404, {"error": "not_found"})
            else:
                self._json(404, {"error": "not_found"})
        except QuestionBusy as error:
            self._json(409, {"error": "question_busy", "detail": str(error)})
        except QuestionCapacity as error:
            self._json(429, {"error": "question_capacity", "detail": str(error)})
        except QuestionInvalidated as error:
            self._json(409, {"error": "question_invalidated", "detail": str(error)})
        except QuestionTimeout as error:
            self._json(504, {"error": "question_timeout", "detail": str(error)})
        except StateConflict as error:
            self._json(409, {"error": "state_conflict", "detail": str(error)})
        except ValidationError as error:
            self._json(400, {"error": "invalid_request", "detail": str(error)})
        except ConfigurationError as error:
            self._json(503, {"error": "not_configured", "detail": str(error)})
        except ModelError as error:
            self._json(502, {"error": "model_error", "detail": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            self._json(500, {"error": "internal_error"})

    def _read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ValidationError("Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as error:
            raise ValidationError("Content-Length must be an integer") from error
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValidationError("request body is too large")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ValidationError("request body must be valid UTF-8 JSON") from error

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.action_token}"
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied, expected)

    @staticmethod
    def _path_character(path: str, prefix: str) -> str:
        raw = path.removeprefix(prefix)
        if not raw or "/" in raw:
            raise ValidationError("character path segment is required")
        try:
            return unquote(raw, errors="strict")
        except UnicodeError as error:
            raise ValidationError("character path segment is invalid") from error

    @staticmethod
    def _reject_query(query: str) -> None:
        if query:
            raise ValidationError("state route does not accept query parameters")

    @staticmethod
    def _watch_query(query: str) -> tuple[int, float]:
        values = parse_qs(query, keep_blank_values=True)
        unknown = set(values) - {"cursor", "timeout"}
        if unknown:
            raise ValidationError(
                f"unsupported query field(s): {', '.join(sorted(unknown))}"
            )
        if any(len(items) != 1 for items in values.values()):
            raise ValidationError("watch query fields may appear only once")
        try:
            cursor = int(values.get("cursor", ["0"])[0])
        except ValueError as error:
            raise ValidationError("cursor must be a nonnegative integer") from error
        try:
            timeout = float(
                values.get("timeout", [str(MAX_WATCH_TIMEOUT_SECONDS)])[0]
            )
        except ValueError as error:
            raise ValidationError("timeout must be a number") from error
        return cursor, timeout

    def log_message(self, format: str, *args: Any) -> None:
        return


def build_server(
    config: ServerConfig | None = None,
    *,
    settings: Settings | None = None,
    model: Model | None = None,
    actions: ActionBroker | None = None,
    action_token: str | None = None,
    world_state: WorldState | None = None,
    inventory: InventoryKnowledge | None = None,
    knowledge: KnowledgeBase | None = None,
    session_hub: SessionHub | None = None,
    watchers: WatcherReducer | None = None,
    timing: Callable[[Mapping[str, object]], None] | None = None,
    controller_manifest: ControllerManifest | None = None,
    character_knowledge: CharacterKnowledge | None = None,
) -> LabHTTPServer:
    resolved = settings if settings is not None else Settings.load()
    selected = config or ServerConfig.from_settings(resolved)
    selected_knowledge = knowledge or KnowledgeBase.from_settings(resolved)
    manifest = controller_manifest or ControllerManifest.load(
        resolved.storage.controller_manifest
    )
    broker = actions or ActionBroker(
        policy=CommandPolicy(manifest),
        audit=JsonlAuditLog(resolved.storage.audit_log),
    )
    token = action_token or load_or_create_action_token(
        resolved.storage.action_token_file
    )
    state = world_state or WorldState()
    selected_inventory = inventory or InventoryKnowledge.from_settings(resolved)
    selected_character_knowledge = character_knowledge or CharacterKnowledge(
        getattr(selected_inventory, "database", None)
    )
    selected_watchers = watchers or WatcherReducer()
    timings = (
        timing
        if timing is not None
        else TimingRecorder(resolved.storage.timing_log)
    )
    assembler = ContextAssembler(
        world_state=state,
        watchers=selected_watchers,
        knowledge=selected_knowledge,
        inventory=selected_inventory,
        character_knowledge=selected_character_knowledge,
    )
    hub = session_hub or SessionHub(
        world_state=state,
        actions=broker,
        inventory=selected_inventory,
        knowledge=selected_knowledge,
        watchers=selected_watchers,
        timing=timings,
        controller_manifest=manifest,
    )
    copilot = Copilot(
        model or model_from_settings(resolved),
        knowledge=selected_knowledge,
        context_assembler=assembler,
        timing=timings,
        custom_instructions=custom_instructions_from_settings(resolved),
        evidence_tools=EvidenceTools(
            hub, character_knowledge=selected_character_knowledge,
            max_result_chars=resolved.selected_profile.evidence_result_chars,
        ),
        evidence_loop=EvidenceLoop(
            max_result_chars=resolved.selected_profile.evidence_result_chars,
            max_evidence_chars=resolved.selected_profile.evidence_total_chars,
        ),
    )
    return LabHTTPServer(
        (selected.host, selected.port),
        copilot,
        broker,
        token,
        state,
        hub,
        selected_watchers,
        selected_character_knowledge,
    )


def model_from_settings(
    settings: Settings,
    *,
    environment: Mapping[str, str] | None = None,
) -> Model:
    """Build the selected existing adapter from one resolved settings object."""

    profile = settings.selected_profile
    provider = settings.providers[profile.provider]
    if provider.kind is ProviderKind.CODEX:
        return CodexExecModel(
            binary=provider.command,
            model=profile.model,
            reasoning_effort=profile.reasoning_effort,
            timeout=profile.timeout_seconds,
        )
    if provider.kind is ProviderKind.OPENAI:
        env = os.environ if environment is None else environment
        credential = (
            env.get(provider.credential_env, "")
            if provider.credential_env is not None
            else ""
        )
        return OpenAIResponsesModel(
            api_key=credential,
            model=profile.model,
            base_url=provider.base_url or "https://api.openai.com/v1",
            reasoning_effort=profile.reasoning_effort,
            timeout=profile.timeout_seconds,
        )
    if provider.kind is ProviderKind.OPENAI_COMPATIBLE:
        env = os.environ if environment is None else environment
        credential = (
            env.get(provider.credential_env, "")
            if provider.credential_env is not None
            else ""
        )
        if profile.model is None:
            raise ConfigurationError(
                "OpenAI-compatible profiles require a configured model"
            )
        return OpenAICompatibleChatModel(
            model=profile.model,
            base_url=provider.base_url or "",
            api_key=credential,
            reasoning_effort=profile.reasoning_effort,
            timeout=profile.timeout_seconds,
        )
    raise ConfigurationError(
        f"provider kind {provider.kind.value!r} is not supported by this runtime yet"
    )


def model_from_environment() -> Model:
    """Compatibility wrapper around the central settings boundary."""

    return model_from_settings(Settings.load())


def custom_instructions_from_settings(settings: Settings) -> str:
    """Load one bounded UTF-8 preference file selected by the active profile."""

    path = settings.selected_profile.instructions_file
    if path is None:
        return ""
    try:
        metadata = path.stat()
    except OSError as error:
        raise ConfigurationError(
            f"cannot read custom instructions file {path}: {error}"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise ConfigurationError(f"custom instructions file is not regular: {path}")
    if metadata.st_size > MAX_CUSTOM_INSTRUCTIONS_BYTES:
        raise ConfigurationError(
            "custom instructions file exceeds "
            f"{MAX_CUSTOM_INSTRUCTIONS_BYTES} bytes: {path}"
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ConfigurationError(
            f"cannot read custom instructions file {path}: {error}"
        ) from error


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        help="settings file (default: LAB_CONFIG or the XDG configuration path)",
    )
    arguments = parser.parse_args(argv)
    try:
        settings = Settings.load(path=arguments.config)
        server = build_server(settings=settings)
    except ConfigurationError as error:
        raise SystemExit(f"configuration error: {error}") from error
    host, port = server.server_address
    configured = "configured" if server.copilot.model_configured else "not configured"
    print(
        f"LAB sidecar listening on http://{host}:{port} "
        f"(backend={server.copilot.model_backend}, {configured})"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
