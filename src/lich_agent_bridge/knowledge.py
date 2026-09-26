"""Character-aware retrieval across curated project notes and a GSWiki mirror."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .gswiki import DEFAULT_API_URL, USER_AGENT, wikitext_to_text
from .settings import GeneralWebProvider, OnlineFallbackPolicy
from .discovery import source_kind
from .wiki_text import research_text as _research_text

if TYPE_CHECKING:
    from .settings import Settings


_WORD = re.compile(r"[a-z0-9][a-z0-9'-]+")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CONTEXT_LABEL = re.compile(
    r"^(?:Previous player question|Clarification|Previous answer context \(unverified\)|Previous reference titles):[ \t]*",
    re.MULTILINE | re.IGNORECASE,
)
_REDIRECT = re.compile(r"^\s*#redirect\s*\[\[([^\]|]+)", re.IGNORECASE)
_CHARACTER_PAGE_BONUS = 30
_TITLE_TERM_BONUS = 20
_SPELL_NUMBER_TITLE_BONUS = 80
_EXACT_PHRASE_BONUS = 12
_CANONICAL_PAGE_BONUS = 24
_REDIRECT_PAGE_PENALTY = 48
_LIVE_CACHE_MAX_ENTRIES = 64
_LIVE_CACHE_TTL_SECONDS = 900.0
_LIVE_MIN_REQUEST_INTERVAL_SECONDS = 1.0
_LIVE_RESPONSE_MAX_BYTES = 524_288
_LIVE_TIMEOUT_SECONDS = 5.0
_BRAVE_SEARCH_API_URL = "https://api.search.brave.com/res/v1/web/search"
_ONLINE_QUERY_NOISE = frozenset({"change", "changes", "mechanics", "updated"})
_CURATED_QUERY_NOISE = _ONLINE_QUERY_NOISE | {"tell", "explain", "describe", "show"}
_DEVELOPMENT_TERMS = frozenset({
    "lab", "lich", "api", "code", "coding", "script", "scripts", "scripting",
    "developer", "development", "architecture", "implementation", "implement",
    "debug", "debugging", "test", "tests", "testing", "repository", "repo",
    "python", "ruby", "mcp", "sessionhub", "actionbroker", "xml", "yard", "deepwiki",
})
_BUILD_QUERY_TERMS = frozenset({"skill", "skills", "training", "trained", "ranks", "rank", "lore", "stats", "build"})
_HISTORY_QUERY_TERMS = frozenset({"history", "historical", "deprecated", "former", "old", "obsolete", "removed", "replaced"})
_RANK_FACT = re.compile(r"\b[0-9]+\s+(?:ranks?|each)\b", re.IGNORECASE)
_BUILD_VOCABULARY = re.compile(r"\b(?:skills?|training|trained|lore|mana control|spell aiming|harness power|arcane symbols)\b", re.IGNORECASE)
_DOCUMENT_DATE = re.compile(r"^(?:Last (?:updated|researched|verified)|Observed):[^\n]{0,120}", re.MULTILINE | re.IGNORECASE)
_STOP_WORDS = {
    "and",
    "are",
    "can",
    "for",
    "has",
    "his",
    "how",
    "its",
    "our",
    "the",
    "was",
    "were",
    "why",
    "will",
    "you",
    "about",
    "after",
    "again",
    "also",
    "could",
    "does",
    "from",
    "have",
    "into",
    "just",
    "like",
    "should",
    "that",
    "their",
    "there",
    "these",
    "they",
    "this",
    "what",
    "when",
    "where",
    "which",
    "with",
    "would",
    "your",
}


class _ResponseTooLarge(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class KnowledgeExcerpt:
    """One bounded passage with enough provenance for inspection."""

    authority: str
    title: str
    text: str
    source: str
    url: str | None = None
    revision_id: int | None = None
    retrieved_at: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeSourceDiagnostic:
    """One source's bounded, user-inspectable retrieval outcome."""

    source: str
    status: str
    detail: str

    def to_mapping(self) -> dict[str, str]:
        return {"source": self.source, "status": self.status, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class KnowledgeSearch(Sequence[KnowledgeExcerpt]):
    """Bounded excerpts and source diagnostics behind the existing search seam."""

    excerpts: tuple[KnowledgeExcerpt, ...]
    diagnostics: tuple[KnowledgeSourceDiagnostic, ...]

    def __len__(self) -> int:
        return len(self.excerpts)

    def __getitem__(
        self, index: int | slice
    ) -> KnowledgeExcerpt | tuple[KnowledgeExcerpt, ...]:
        return self.excerpts[index]

    def __iter__(self) -> Iterator[KnowledgeExcerpt]:
        return iter(self.excerpts)


class LiveGSWikiSource:
    """Small, bounded read-through cache around the public GSWiki API."""

    def __init__(
        self,
        *,
        cache_path: Path | None,
        api_url: str = DEFAULT_API_URL,
        timeout_seconds: float = _LIVE_TIMEOUT_SECONDS,
        max_response_bytes: int = _LIVE_RESPONSE_MAX_BYTES,
        cache_ttl_seconds: float = _LIVE_CACHE_TTL_SECONDS,
        min_request_interval_seconds: float = _LIVE_MIN_REQUEST_INTERVAL_SECONDS,
        opener: Callable[..., Any] = urlopen,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._cache_path = None if cache_path is None else Path(cache_path)
        self._api_url = api_url
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._cache_ttl_seconds = cache_ttl_seconds
        self._min_request_interval_seconds = min_request_interval_seconds
        self._opener = opener
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._last_request_at: float | None = None
        self._memory_cache: dict[str, Mapping[str, Any]] = {}

    def search(
        self, question: str, terms: tuple[str, ...]
    ) -> tuple[list[KnowledgeExcerpt], KnowledgeSourceDiagnostic]:
        if not terms:
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="empty", detail="question has no searchable terms"
            )
        query = _live_query(terms)
        if not query:
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="empty", detail="question has no searchable terms"
            )
        key = query.casefold() + (":historical" if _HISTORY_QUERY_TERMS.intersection(terms) else "")
        cached = self._read_cache().get(key)
        current = self._now()
        if cached is not None and _cache_is_fresh(
            cached, now=current, ttl_seconds=self._cache_ttl_seconds
        ):
            excerpts = [item for item in _cached_excerpts(cached) if _usable_page(item.text, terms)]
            if excerpts:
                return excerpts, KnowledgeSourceDiagnostic(
                    source="live_gswiki",
                    status="success",
                    detail=f"cache hit with {len(excerpts)} excerpt(s)",
                )
        if self._last_request_at is not None and (
            self._monotonic() - self._last_request_at
            < self._min_request_interval_seconds
        ):
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki",
                status="rate_limited",
                detail="live GSWiki lookup is locally rate limited",
            )
        self._last_request_at = self._monotonic()
        try:
            payload = self._request(query)
            excerpts = self._excerpts(payload, terms=terms, retrieved_at=current, preserve_headings=True)
        except HTTPError as error:
            status = "rate_limited" if error.code == 429 else "unavailable"
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status=status, detail=f"GSWiki returned HTTP {error.code}"
            )
        except (URLError, OSError, TimeoutError):
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="unavailable", detail="GSWiki lookup is unavailable"
            )
        except _ResponseTooLarge:
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="oversized", detail="GSWiki response exceeded configured limit"
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="unreadable", detail="GSWiki returned an invalid response"
            )
        if not excerpts:
            diagnostic = KnowledgeSourceDiagnostic(
                source="live_gswiki", status="empty", detail="no matching live GSWiki excerpt"
            )
        else:
            self._write_cache(key, excerpts, retrieved_at=current)
            diagnostic = KnowledgeSourceDiagnostic(
                source="live_gswiki", status="success", detail=f"matched {len(excerpts)} excerpt(s)"
            )
        return excerpts, diagnostic

    def _request(self, query: str) -> Mapping[str, Any]:
        parameters = {
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "0",
            "gsrlimit": "4",
            "redirects": "1",
            "prop": "revisions",
            "rvprop": "ids|timestamp|content",
            "rvslots": "main",
            "format": "json",
            "formatversion": "2",
        }
        return self._request_parameters(parameters)

    def _request_parameters(self, parameters: Mapping[str, str]) -> Mapping[str, Any]:
        request = Request(
            f"{self._api_url}?{urlencode(parameters)}",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        with self._opener(request, timeout=self._timeout_seconds) as response:
            data = response.read(self._max_response_bytes + 1)
        if len(data) > self._max_response_bytes:
            raise _ResponseTooLarge("GSWiki response exceeded configured limit")
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, Mapping) or "error" in payload:
            raise ValueError("GSWiki response is invalid")
        return payload

    def read(self, title: str) -> tuple[KnowledgeExcerpt | None, KnowledgeSourceDiagnostic]:
        """Read one application-selected page identity, never an arbitrary URL."""
        # Exact-page snapshots are distinct from query results. Preserve title
        # case: MediaWiki page names can differ beyond their first character.
        key = "page:" + title.replace("_", " ").strip()
        current = self._now()
        cached = self._read_cache().get(key)
        if cached is not None and _cache_is_fresh(
            cached, now=current, ttl_seconds=self._cache_ttl_seconds
        ):
            excerpts = _cached_excerpts(cached)
            if excerpts:
                return excerpts[0], KnowledgeSourceDiagnostic(
                    "live_gswiki", "success", "selected page cache hit; original retrieval time retained"
                )
        if self._last_request_at is not None and (
            self._monotonic() - self._last_request_at < self._min_request_interval_seconds
        ):
            return None, KnowledgeSourceDiagnostic("live_gswiki", "rate_limited", "exact-page refresh is locally rate limited")
        self._last_request_at = self._monotonic()
        try:
            payload = self._request_parameters({
                "action": "query", "titles": title, "redirects": "1",
                "prop": "revisions", "rvprop": "ids|timestamp|content",
                "rvslots": "main", "format": "json", "formatversion": "2",
            })
            excerpts = self._excerpts(payload, terms=(), retrieved_at=current, preserve_headings=True)
        except HTTPError as error:
            return None, KnowledgeSourceDiagnostic("live_gswiki", "rate_limited" if error.code == 429 else "unavailable", "exact-page refresh failed")
        except (URLError, OSError, TimeoutError, ValueError, TypeError, UnicodeError):
            return None, KnowledgeSourceDiagnostic("live_gswiki", "unavailable", "exact-page refresh unavailable")
        if not excerpts:
            return None, KnowledgeSourceDiagnostic("live_gswiki", "empty", "selected page was not returned")
        self._write_cache(key, excerpts[:1], retrieved_at=current)
        return excerpts[0], KnowledgeSourceDiagnostic("live_gswiki", "success", "selected page revision retrieved")

    @staticmethod
    def _excerpts(
        payload: Mapping[str, Any], *, terms: tuple[str, ...], retrieved_at: datetime,
        preserve_headings: bool = False,
    ) -> list[KnowledgeExcerpt]:
        pages = payload.get("query", {}).get("pages", [])
        if not isinstance(pages, list):
            raise ValueError("GSWiki pages are invalid")
        stamp = retrieved_at.astimezone(UTC).isoformat()
        ranked: list[tuple[int, KnowledgeExcerpt]] = []
        for page in pages[:4]:
            if not isinstance(page, Mapping):
                continue
            title = page.get("title")
            revisions = page.get("revisions")
            if not isinstance(title, str) or not isinstance(revisions, list) or not revisions:
                continue
            revision = revisions[0]
            if not isinstance(revision, Mapping):
                continue
            slots = revision.get("slots", {})
            main = slots.get("main", {}) if isinstance(slots, Mapping) else {}
            content = main.get("content") if isinstance(main, Mapping) else None
            if not isinstance(content, str):
                continue
            if not _usable_page(content, terms):
                continue
            text = _research_text(content) if preserve_headings else wikitext_to_text(content)
            if not text:
                continue
            url = f"https://gswiki.play.net/{quote(title.replace(' ', '_'), safe='/:()')}"
            revision_id = revision.get("revid")
            score = _title_score(title, terms)
            score += (
                -_REDIRECT_PAGE_PENALTY
                if _redirect_target(content) is not None
                else _CANONICAL_PAGE_BONUS
            )
            ranked.append(
                (
                    score,
                    KnowledgeExcerpt(
                    authority="live GSWiki API",
                    title=title,
                    text=text,
                    source=url,
                    url=url,
                    revision_id=revision_id if isinstance(revision_id, int) else None,
                    retrieved_at=stamp,
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1].title))
        return [excerpt for _, excerpt in ranked]

    def _read_cache(self) -> dict[str, Mapping[str, Any]]:
        if self._cache_path is None:
            return dict(self._memory_cache)
        try:
            raw = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            return dict(self._memory_cache)
        if not isinstance(raw, Mapping):
            return dict(self._memory_cache)
        entries = raw.get("entries", {})
        if not isinstance(entries, Mapping):
            return dict(self._memory_cache)
        merged = {str(key): value for key, value in entries.items() if isinstance(value, Mapping)}
        for key, value in self._memory_cache.items():
            disk = merged.get(key)
            if disk is None or str(value.get("retrieved_at", "")) > str(disk.get("retrieved_at", "")):
                merged[key] = value
        return merged

    def _write_cache(
        self, key: str, excerpts: Sequence[KnowledgeExcerpt], *, retrieved_at: datetime
    ) -> None:
        entries = self._read_cache()
        entries[key] = {
            "retrieved_at": retrieved_at.astimezone(UTC).isoformat(),
            "excerpts": [
                {
                    "title": item.title,
                    "text": item.text,
                    "source": item.source,
                    "url": item.url,
                    "revision_id": item.revision_id,
                    "retrieved_at": item.retrieved_at,
                }
                for item in excerpts
            ],
        }
        selected = sorted(
            entries.items(),
            key=lambda item: str(item[1].get("retrieved_at", "")),
            reverse=True,
        )[:_LIVE_CACHE_MAX_ENTRIES]
        self._memory_cache = dict(selected)
        if self._cache_path is None:
            return
        document = {"entries": self._memory_cache}
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{self._cache_path.name}.", dir=self._cache_path.parent
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, self._cache_path)
        except OSError:
            try:
                os.unlink(temporary)
            except (OSError, UnboundLocalError):
                pass


class BraveWebSearchSource:
    """Optional, credentialed general-web fallback with bounded result snippets."""

    def __init__(
        self,
        *,
        enabled: bool,
        credential_env: str | None,
        environment: Mapping[str, str] | None = None,
        api_url: str = _BRAVE_SEARCH_API_URL,
        timeout_seconds: float = _LIVE_TIMEOUT_SECONDS,
        max_response_bytes: int = _LIVE_RESPONSE_MAX_BYTES,
        min_request_interval_seconds: float = _LIVE_MIN_REQUEST_INTERVAL_SECONDS,
        opener: Callable[..., Any] = urlopen,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self._enabled = enabled
        self._credential_env = credential_env
        self._environment = os.environ if environment is None else environment
        self._api_url = api_url
        self._timeout_seconds = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._min_request_interval_seconds = min_request_interval_seconds
        self._opener = opener
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic
        self._last_request_at: float | None = None

    def search(
        self, question: str, terms: tuple[str, ...]
    ) -> tuple[list[KnowledgeExcerpt], KnowledgeSourceDiagnostic]:
        if not self._enabled:
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="disabled", detail="general web fallback is disabled by the selected profile"
            )
        credential = (
            None
            if self._credential_env is None
            else self._environment.get(self._credential_env, "").strip()
        )
        if not credential:
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="unavailable", detail="general web credential is not available"
            )
        if not terms:
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="empty", detail="question has no searchable terms"
            )
        if self._last_request_at is not None and (
            self._monotonic() - self._last_request_at
            < self._min_request_interval_seconds
        ):
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="rate_limited", detail="general web lookup is locally rate limited"
            )
        self._last_request_at = self._monotonic()
        try:
            request = Request(
                f"{self._api_url}?{urlencode({'q': ' '.join(question.split())[:1_000], 'count': '4'})}",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": credential,
                    "User-Agent": USER_AGENT,
                },
            )
            with self._opener(request, timeout=self._timeout_seconds) as response:
                data = response.read(self._max_response_bytes + 1)
            if len(data) > self._max_response_bytes:
                raise _ResponseTooLarge()
            payload = json.loads(data.decode("utf-8"))
            excerpts = self._excerpts(payload, retrieved_at=self._now())
        except HTTPError as error:
            status = "rate_limited" if error.code == 429 else "unavailable"
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status=status, detail=f"general web provider returned HTTP {error.code}"
            )
        except _ResponseTooLarge:
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="oversized", detail="general web response exceeded configured limit"
            )
        except (URLError, OSError, TimeoutError):
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="unavailable", detail="general web lookup is unavailable"
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="unreadable", detail="general web provider returned an invalid response"
            )
        return excerpts, KnowledgeSourceDiagnostic(
            source="general_web",
            status="success" if excerpts else "empty",
            detail=(f"matched {len(excerpts)} excerpt(s)" if excerpts else "no matching web excerpt"),
        )

    @staticmethod
    def _excerpts(payload: Mapping[str, Any], *, retrieved_at: datetime) -> list[KnowledgeExcerpt]:
        results = payload.get("web", {}).get("results", [])
        if not isinstance(results, list):
            raise ValueError("general web results are invalid")
        stamp = retrieved_at.astimezone(UTC).isoformat()
        excerpts: list[KnowledgeExcerpt] = []
        for result in results[:4]:
            if not isinstance(result, Mapping):
                continue
            title = result.get("title")
            url = result.get("url")
            description = result.get("description")
            if not all(isinstance(value, str) and value for value in (title, url, description)):
                continue
            if not url.startswith(("https://", "http://")):
                continue
            excerpts.append(
                KnowledgeExcerpt(
                    authority="general web search (Brave)",
                    title=title,
                    text=description,
                    source=url,
                    url=url,
                    retrieved_at=stamp,
                )
            )
        return excerpts


class KnowledgeBase:
    """Hide multi-source retrieval behind one character-aware search interface."""

    def __init__(
        self,
        *,
        wiki_root: Path,
        gswiki_database: Path | None = None,
        max_excerpts: int = 6,
        max_characters: int = 12_000,
        max_excerpt_characters: int = 2_400,
        mirror_max_age_hours: float = 168.0,
        online_fallback: OnlineFallbackPolicy = OnlineFallbackPolicy.DISABLED,
        live_gswiki: LiveGSWikiSource | None = None,
        general_web: BraveWebSearchSource | None = None,
        semantic_reranker=None,
        now: Callable[[], datetime] | None = None,
    ):
        self._wiki_root = Path(wiki_root)
        self._gswiki_database = (
            Path(gswiki_database) if gswiki_database is not None else None
        )
        self._max_excerpts = max_excerpts
        self._max_characters = max_characters
        self._max_excerpt_characters = max_excerpt_characters
        self._mirror_max_age_hours = mirror_max_age_hours
        self._online_fallback = online_fallback
        self._live_gswiki = live_gswiki
        self._general_web = general_web
        self._semantic_reranker = semantic_reranker
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    def from_environment(cls) -> "KnowledgeBase":
        """Compatibility wrapper around the central settings module."""

        from .settings import Settings

        return cls.from_settings(Settings.load())

    @classmethod
    def from_settings(cls, settings: "Settings") -> "KnowledgeBase":
        """Build from the already-resolved application configuration."""

        semantic = None
        if settings.knowledge.semantic_model_directory is not None:
            from .semantic import SemanticReranker
            semantic = SemanticReranker(settings.knowledge.semantic_model_directory)
        return cls(
            wiki_root=settings.knowledge.wiki_root,
            gswiki_database=settings.knowledge.gswiki_database,
            mirror_max_age_hours=settings.knowledge.mirror_max_age_hours,
            online_fallback=settings.knowledge.online_fallback,
            semantic_reranker=semantic,
            live_gswiki=LiveGSWikiSource(
                cache_path=settings.storage.state_directory / "live-gswiki-cache.json"
            ),
            general_web=(
                BraveWebSearchSource(
                    enabled=settings.selected_profile.web_search,
                    credential_env=settings.knowledge.general_web_credential_env,
                )
                if settings.knowledge.general_web_provider is GeneralWebProvider.BRAVE
                else None
            ),
        )

    def open_research(self, *, character: str, max_chars: int = 6000):
        """Create an isolated source registry for one admitted question."""
        from .research import ResearchSession

        return ResearchSession(self, character=character, max_chars=max_chars)

    def search(self, *, character: str, question: str) -> KnowledgeSearch:
        """Return bounded excerpts and truthful diagnostics for each local source."""

        terms = _terms(question)
        curated, curated_diagnostic = self._search_curated(
            character=character, terms=terms
        )
        external, external_diagnostic = self._search_gswiki(terms=terms)
        live, live_diagnostic = self._search_live_gswiki_if_needed(
            question=question,
            terms=terms,
            local_excerpts=external,
            local_diagnostic=external_diagnostic,
        )
        web, web_diagnostic = self._search_general_web_if_needed(
            question=question,
            terms=terms,
            local_excerpts=external,
            local_diagnostic=external_diagnostic,
            live_excerpts=live,
            live_diagnostic=live_diagnostic,
        )

        # Deduplicate before applying the result budget: fresh canonical evidence
        # must replace an older mirror copy, even when their authority labels differ.
        unique: dict[str, KnowledgeExcerpt] = {}
        for excerpt in [*curated, *external, *live, *web]:
            identity = _source_identity(excerpt)
            previous = unique.get(identity)
            if previous is None or _newer_evidence(excerpt, previous):
                unique[identity] = excerpt
        candidates = sorted(
            unique.values(),
            key=lambda excerpt: -_evidence_score(excerpt, terms, character),
        )
        selected: list[KnowledgeExcerpt] = []
        used = 0
        for excerpt in candidates:
            text = excerpt.text[: self._max_excerpt_characters].strip()
            if not text:
                continue
            cost = len(text) + len(excerpt.title) + len(excerpt.source) + 80
            if selected and (
                len(selected) >= self._max_excerpts
                or used + cost > self._max_characters
            ):
                break
            selected.append(
                KnowledgeExcerpt(
                    authority=excerpt.authority,
                    title=excerpt.title,
                    text=text,
                    source=excerpt.source,
                    url=excerpt.url,
                    revision_id=excerpt.revision_id,
                    retrieved_at=excerpt.retrieved_at,
                )
            )
            used += cost
        return KnowledgeSearch(
            excerpts=tuple(selected),
            diagnostics=(
                curated_diagnostic,
                external_diagnostic,
                live_diagnostic,
                web_diagnostic,
            ),
        )

    def _search_live_gswiki_if_needed(
        self,
        *,
        question: str,
        terms: tuple[str, ...],
        local_excerpts: Sequence[KnowledgeExcerpt],
        local_diagnostic: KnowledgeSourceDiagnostic,
    ) -> tuple[list[KnowledgeExcerpt], KnowledgeSourceDiagnostic]:
        if self._online_fallback is OnlineFallbackPolicy.DISABLED:
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="disabled", detail="online fallback is disabled"
            )
        if self._live_gswiki is None:
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="unavailable", detail="live GSWiki source is not configured"
            )
        local_needs_backup = local_diagnostic.status in {"missing", "stale", "empty"}
        if not local_needs_backup and _sufficiently_relevant(local_excerpts, terms):
            return [], KnowledgeSourceDiagnostic(
                source="live_gswiki", status="not_needed", detail="local GSWiki result is sufficiently relevant"
            )
        return self._live_gswiki.search(question, terms)

    def _search_general_web_if_needed(
        self,
        *,
        question: str,
        terms: tuple[str, ...],
        local_excerpts: Sequence[KnowledgeExcerpt],
        local_diagnostic: KnowledgeSourceDiagnostic,
        live_excerpts: Sequence[KnowledgeExcerpt],
        live_diagnostic: KnowledgeSourceDiagnostic,
    ) -> tuple[list[KnowledgeExcerpt], KnowledgeSourceDiagnostic]:
        if self._general_web is None:
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="disabled", detail="general web provider is not configured"
            )
        if local_diagnostic.status == "success" and _sufficiently_relevant(local_excerpts, terms):
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="not_needed", detail="local GSWiki result is sufficiently relevant"
            )
        live_needs_backup = live_diagnostic.status in {
            "disabled",
            "empty",
            "unavailable",
            "unreadable",
            "oversized",
            "rate_limited",
        }
        if not live_needs_backup and _sufficiently_relevant(live_excerpts, terms):
            return [], KnowledgeSourceDiagnostic(
                source="general_web", status="not_needed", detail="live GSWiki result is sufficiently relevant"
            )
        return self._general_web.search(question, terms)

    def _search_curated(
        self, *, character: str, terms: tuple[str, ...]
    ) -> tuple[list[KnowledgeExcerpt], KnowledgeSourceDiagnostic]:
        if not self._wiki_root.exists():
            return [], KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="missing",
                detail="wiki root is not present",
            )
        if not self._wiki_root.is_dir():
            return [], KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="unreadable",
                detail="wiki root is not a directory",
            )

        character_key = character.casefold()
        development_query = bool(_DEVELOPMENT_TERMS.intersection(terms))
        ranked: list[tuple[int, KnowledgeExcerpt]] = []
        character_candidates: list[tuple[int, KnowledgeExcerpt]] = []
        try:
            paths = tuple(self._wiki_root.rglob("*.md"))
        except OSError:
            return [], KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="unreadable",
                detail="wiki root cannot be scanned",
            )
        if not paths:
            return [], KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="empty",
                detail="no Markdown files were found",
            )

        readable = 0
        unreadable = 0
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                unreadable += 1
                continue
            readable += 1
            relative = path.relative_to(self._wiki_root).as_posix()
            relative_key = relative.casefold()
            if relative_key.startswith("project/") and not development_query:
                continue
            if (
                relative_key.startswith("characters/")
                and relative_key != f"characters/{character_key}.md"
                and not relative_key.startswith(f"characters/{character_key}-")
            ):
                continue
            character_page = relative_key == f"characters/{character_key}.md"
            character_topic = relative_key.startswith(f"characters/{character_key}-")
            document_date = _DOCUMENT_DATE.search(content)
            for heading, text in _markdown_chunks(content):
                haystack = f"{heading}\n{text}".casefold()
                score = _score(haystack, terms) + _title_score(heading, terms)
                build_score = _recorded_build_score(heading, text, terms) if character_page or character_topic else 0
                score += build_score
                if build_score:
                    date = document_date.group(0) if document_date else "Record date not supplied."
                    dated_text = text if date in text else f"{date}\n{text}"
                    text = f"Historical character record; not a live skills observation.\n{dated_text}"
                excerpt = KnowledgeExcerpt(
                    authority="curated project knowledge",
                    title=heading or path.stem,
                    text=text,
                    source=f"wiki/{relative}",
                )
                if character_page:
                    character_candidates.append(
                        (score + _CHARACTER_PAGE_BONUS, excerpt)
                    )
                elif score > 0 and (
                    development_query
                    or build_score
                    or _curated_gameplay_match(heading, text, terms)
                ):
                    ranked.append((score, excerpt))
        if character_candidates:
            ranked.append(
                max(
                    character_candidates,
                    key=lambda item: (item[0], item[1].title, item[1].source),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1].source, item[1].title))
        excerpts = [excerpt for _, excerpt in ranked]
        if not readable:
            diagnostic = KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="unreadable",
                detail="no Markdown files could be read",
            )
        elif excerpts:
            suffix = "" if not unreadable else f"; skipped {unreadable} unreadable file(s)"
            diagnostic = KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="success",
                detail=f"matched {len(excerpts)} excerpt(s){suffix}",
            )
        else:
            diagnostic = KnowledgeSourceDiagnostic(
                source="curated_wiki",
                status="empty",
                detail="no matching curated excerpt",
            )
        return excerpts, diagnostic

    def _research_gswiki(self, *, terms: tuple[str, ...]):
        """Rank bounded indexed passages, falling back to legacy mirror discovery.

        This read-only path never builds or repairs derived data. Full normalized
        documents are fetched separately after the research source shortlist.
        """
        from .passage_index import PassageIndexUnavailable, query_passages

        database = self._gswiki_database
        reason = 'missing'
        if database is not None and database.is_file() and terms:
            try:
                connection = sqlite3.connect(f'file:{database}?mode=ro', uri=True)
                try:
                    connection.row_factory = sqlite3.Row
                    hits = query_passages(connection, terms, namespaces=(0, 4), limit=36)
                    row = connection.execute("SELECT value FROM metadata WHERE key = 'last_sync'").fetchone()
                    stamp = None if row is None else str(row[0])
                finally:
                    connection.close()
                grouped = {}
                for hit in hits:
                    grouped.setdefault((hit.url, hit.revision_id), []).append(hit)
                excerpts = []
                for passages in grouped.values():
                    first = passages[0]
                    # Match the existing source ranker's input using bounded
                    # passage text, not a freshly normalized full wiki page.
                    preview = '\n\n'.join('\n'.join('# ' + heading for heading in hit.heading_path)
                                            + '\n' + hit.text for hit in passages)
                    excerpts.append(KnowledgeExcerpt('external GSWiki reference', first.title,
                                                     preview, first.url, url=first.url,
                                                     revision_id=first.revision_id, retrieved_at=stamp))
                freshness = _mirror_diagnostic(stamp, self._mirror_max_age_hours, self._now())
                diagnostic = KnowledgeSourceDiagnostic('local_gswiki',
                    'stale' if freshness.status == 'stale' else ('success' if excerpts else 'empty'),
                    f'passage index: {len(hits)} matches in {len(excerpts)} sources'
                    + ('; ' + freshness.detail if freshness.status == 'stale' else ''))
                return excerpts, diagnostic, grouped
            except (sqlite3.Error, PassageIndexUnavailable) as error:
                reason = str(error) or 'unavailable'
        excerpts, diagnostic = self._search_gswiki(terms=terms, full_documents=True)
        diagnostic = KnowledgeSourceDiagnostic(diagnostic.source, diagnostic.status,
            diagnostic.detail + '; legacy page search (passage index ' + reason[:160] + ')')
        return excerpts, diagnostic, {}

    def _rerank_research(self, ranked, passage_hits, *, query, terms, check):
        """Optional local relevance only; never broadens discovery or read rights."""
        if self._semantic_reranker is None:
            return ranked, None
        from .discovery import rerank_semantic
        from .passage_index import PassageIndexUnavailable, ranking_passage_bodies
        from .semantic import SemanticPassage, SemanticUnavailable
        import hashlib

        eligible = {(item.excerpt.source, item.excerpt.revision_id) for item in ranked
                    if item.scope == 'reference'}
        hits = tuple(hit for identity, group in passage_hits.items() if identity in eligible
                     for hit in group)
        if not hits:
            return ranked, {'source': 'semantic_reranking', 'status': 'not_needed',
                            'detail': 'No indexed reference candidates; lexical order retained.'}
        check()
        try:
            connection = sqlite3.connect(f'file:{self._gswiki_database}?mode=ro', uri=True)
            try:
                connection.execute('BEGIN')
                bodies = ranking_passage_bodies(connection, hits, check=check)
            finally:
                connection.close()
        except (sqlite3.Error, PassageIndexUnavailable):
            return ranked, {'source': 'semantic_reranking', 'status': 'input_unavailable',
                            'detail': 'Indexed ranking ranges unavailable or over allowance; lexical order retained.'}
        passages = []
        for hit, body in zip(hits, bodies):
            identity = (hit.url, hit.revision_id, hit.normalizer_version,
                        hit.source_fingerprint, hit.document_fingerprint, hit.start, hit.end)
            key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
            passages.append(SemanticPassage(key, hit.title, hit.heading_path, body))
        check()
        try:
            result = self._semantic_reranker.score(query, passages, check=check)
        except SemanticUnavailable as error:
            return ranked, {'source': 'semantic_reranking', 'status': error.code,
                            'detail': error.reason + '; lexical order retained.'}
        check()
        scores = {}
        for hit, passage in zip(hits, passages):
            identity = (hit.url, hit.revision_id)
            scores[identity] = max(scores.get(identity, -1.0), result.scores[passage.key])
        return rerank_semantic(ranked, query, scores), {
            'source': 'semantic_reranking', 'status': 'success',
            'detail': 'Local source reranking with explicit-name protection; read policy unchanged.',
            'metrics': result.stats}

    def _load_research_document(self, hit, *, retrieved_at):
        """Load one selected immutable indexed snapshot, checking range identity."""
        from .passage_index import PassageIndexUnavailable, load_document

        connection = sqlite3.connect(f'file:{self._gswiki_database}?mode=ro', uri=True)
        try:
            connection.row_factory = sqlite3.Row
            document = load_document(connection, hit.page_id)
        finally:
            connection.close()
        if document is None or any(getattr(document, key) != getattr(hit, key) for key in
                ('page_id', 'title', 'url', 'namespace', 'revision_id', 'normalizer_version',
                 'source_fingerprint', 'document_fingerprint')):
            raise PassageIndexUnavailable('selected indexed snapshot changed; search again')
        return KnowledgeExcerpt('external GSWiki reference', document.title, document.text,
                                document.url, url=document.url, revision_id=document.revision_id,
                                retrieved_at=retrieved_at)

    def _research_document_passages(self, hit, excerpt, *, terms, limit=3):
        """Find focused ranges without reloading a selected normalized source."""
        from .passage_index import PassageIndexUnavailable, document_passages

        if (excerpt.title != hit.title or excerpt.source != hit.url
                or excerpt.revision_id != hit.revision_id):
            raise PassageIndexUnavailable('selected excerpt belongs to a different snapshot')
        connection = sqlite3.connect(f'file:{self._gswiki_database}?mode=ro', uri=True)
        try:
            connection.execute('BEGIN')
            return document_passages(connection, hit, excerpt.text, terms, limit=limit)
        finally:
            connection.close()

    def _search_gswiki(
        self, *, terms: tuple[str, ...], full_documents: bool = False
    ) -> tuple[list[KnowledgeExcerpt], KnowledgeSourceDiagnostic]:
        database = self._gswiki_database
        if database is None or not database.exists():
            return [], KnowledgeSourceDiagnostic(
                source="local_gswiki",
                status="missing",
                detail="GSWiki mirror is not present",
            )
        if not database.is_file():
            return [], KnowledgeSourceDiagnostic(
                source="local_gswiki",
                status="unreadable",
                detail="GSWiki mirror is not a file",
            )
        if not terms:
            return [], KnowledgeSourceDiagnostic(
                source="local_gswiki",
                status="empty",
                detail="question has no searchable terms",
            )

        query = " OR ".join(f'"{term}"*' for term in terms[:10])
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
            if not {"pages", "pages_fts", "metadata"}.issubset(tables):
                return [], KnowledgeSourceDiagnostic(
                    source="local_gswiki",
                    status="unreadable",
                    detail="GSWiki mirror schema is incomplete",
                )
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'last_sync'"
            ).fetchone()
            mirror_sync = None if row is None else str(row[0])
            rows = connection.execute(
                """
                SELECT p.page_id, p.title, p.plain_text, p.wikitext, p.url,
                       p.revision_id, p.namespace,
                       bm25(pages_fts, 8.0, 1.0) AS rank
                  FROM pages_fts
                  JOIN pages AS p ON p.page_id = pages_fts.rowid
                 WHERE pages_fts MATCH ?
                   AND p.namespace IN (0, 4)
                 ORDER BY rank
                 LIMIT 12
                """,
                (query,),
            ).fetchall()
            seen_ids = {int(item["page_id"]) for item in rows}
            numbers = [term for term in terms if term.isdecimal()][:4]
            if numbers or full_documents:
                # Body matches can fill the general FTS shortlist. A separate
                # indexed title lookup keeps named research pages in contention.
                # Legacy excerpt callers keep their numeric-only lookup.
                title_query = " OR ".join(f'title : "{term}"' for term in (terms[:10] if full_documents else numbers))
                title_rank = 'bm25(pages_fts, 8.0, 1.0)' if full_documents else '0.0'
                title_order = 'ORDER BY rank, p.title, p.page_id' if full_documents else ''
                title_rows = connection.execute(
                    f"""
                    SELECT p.page_id, p.title, p.plain_text, p.wikitext, p.url,
                           p.revision_id, p.namespace, {title_rank} AS rank
                      FROM pages_fts
                      JOIN pages AS p ON p.page_id = pages_fts.rowid
                     WHERE pages_fts MATCH ? AND p.namespace IN (0, 4)
                     {title_order} LIMIT 12
                    """, (title_query,),
                ).fetchall()
                rows.extend(item for item in title_rows if int(item["page_id"]) not in seen_ids)
                seen_ids.update(int(item["page_id"]) for item in title_rows)
            if full_documents:
                # A body match in a general rules page must remain in contention
                # even when catalogs fill both the general and title shortlists.
                # This is an indexed FTS lane, not an unbounded document scan.
                connection.create_function('discovery_kind', 1, source_kind, deterministic=True)
                reference_rows = connection.execute(
                    """
                    SELECT p.page_id, p.title, p.plain_text, p.wikitext, p.url,
                           p.revision_id, p.namespace, bm25(pages_fts, 8.0, 1.0) AS rank
                      FROM pages_fts JOIN pages AS p ON p.page_id = pages_fts.rowid
                     WHERE pages_fts MATCH ? AND p.namespace IN (0, 4)
                       AND discovery_kind(p.title) != 'catalog'
                     ORDER BY rank, p.title, p.page_id LIMIT 12
                    """, (query,),
                ).fetchall()
                rows.extend(item for item in reference_rows if int(item['page_id']) not in seen_ids)
                seen_ids.update(int(item['page_id']) for item in reference_rows)
            canonical_rows = []
            for item in rows:
                target = _redirect_target(str(item["wikitext"]))
                if target is None:
                    continue
                canonical = connection.execute(
                    """
                    SELECT page_id, title, plain_text, wikitext, url, revision_id,
                           namespace, 0.0 AS rank
                      FROM pages
                     WHERE title = ? AND namespace IN (0, 4)
                     LIMIT 1
                    """,
                    (target,),
                ).fetchone()
                if (
                    canonical is not None
                    and int(canonical["page_id"]) not in seen_ids
                ):
                    canonical_rows.append(canonical)
                    seen_ids.add(int(canonical["page_id"]))
            rows = [*rows, *canonical_rows]
            mirror_diagnostic = _mirror_diagnostic(
                mirror_sync,
                self._mirror_max_age_hours,
                self._now(),
            )
        except sqlite3.Error:
            return [], KnowledgeSourceDiagnostic(
                source="local_gswiki",
                status="unreadable",
                detail="GSWiki mirror cannot be read",
            )
        finally:
            if "connection" in locals():
                connection.close()

        if not rows:
            detail = "no matching GSWiki excerpt"
            if mirror_diagnostic.status == "stale":
                detail += "; " + mirror_diagnostic.detail
            return [], KnowledgeSourceDiagnostic(
                source="local_gswiki",
                status=(
                    mirror_diagnostic.status
                    if mirror_diagnostic.status == "stale"
                    else "empty"
                ),
                detail=detail,
            )

        ranked: list[tuple[int, float, KnowledgeExcerpt]] = []
        for row in rows:
            if not _usable_page(str(row["wikitext"]), terms):
                continue
            text = (
                _research_text(str(row["wikitext"])) or str(row["plain_text"])
                if full_documents else _best_text_window(row["plain_text"], terms)
            )
            score = (
                _score(text.casefold(), terms)
                + _phrase_bonus(text, terms)
                + _title_score(row["title"], terms)
            )
            score += (
                -_REDIRECT_PAGE_PENALTY
                if _redirect_target(str(row["wikitext"])) is not None
                else _CANONICAL_PAGE_BONUS
            )
            ranked.append(
                (
                    score,
                    float(row["rank"]),
                    KnowledgeExcerpt(
                        authority="external GSWiki reference",
                        title=row["title"],
                        text=text,
                        source=row["url"],
                        url=row["url"],
                        revision_id=row["revision_id"],
                        retrieved_at=mirror_sync if full_documents else None,
                    ),
                )
            )
        ranked.sort(key=lambda item: (-item[0], item[1], item[2].title))
        excerpts = [excerpt for _, _, excerpt in ranked]
        if mirror_diagnostic.status == "stale":
            diagnostic = KnowledgeSourceDiagnostic(
                source="local_gswiki", status="stale", detail=mirror_diagnostic.detail
            )
        else:
            diagnostic = KnowledgeSourceDiagnostic(
                source="local_gswiki", status="success", detail=f"matched {len(excerpts)} excerpt(s)"
            )
        return excerpts, diagnostic


def _cache_is_fresh(
    entry: Mapping[str, Any], *, now: datetime, ttl_seconds: float
) -> bool:
    raw_stamp = entry.get("retrieved_at")
    if not isinstance(raw_stamp, str):
        return False
    try:
        stamp = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    if stamp.tzinfo is None:
        return False
    age_seconds = (now.astimezone(UTC) - stamp.astimezone(UTC)).total_seconds()
    return 0 <= age_seconds <= ttl_seconds


def _cached_excerpts(entry: Mapping[str, Any]) -> list[KnowledgeExcerpt]:
    values = entry.get("excerpts")
    if not isinstance(values, list):
        return []
    excerpts: list[KnowledgeExcerpt] = []
    for value in values[:4]:
        if not isinstance(value, Mapping):
            continue
        title = value.get("title")
        text = value.get("text")
        source = value.get("source")
        if not all(isinstance(item, str) and item for item in (title, text, source)):
            continue
        revision_id = value.get("revision_id")
        excerpts.append(
            KnowledgeExcerpt(
                authority="live GSWiki API",
                title=title,
                text=text,
                source=source,
                url=value.get("url") if isinstance(value.get("url"), str) else None,
                revision_id=revision_id if isinstance(revision_id, int) else None,
                retrieved_at=(
                    value.get("retrieved_at")
                    if isinstance(value.get("retrieved_at"), str)
                    else None
                ),
            )
        )
    return excerpts


def _source_identity(excerpt: KnowledgeExcerpt) -> str:
    url = excerpt.url or excerpt.source
    parsed = urlsplit(url)
    if parsed.hostname == "gswiki.play.net":
        return "gswiki:" + unquote(parsed.path).replace(" ", "_").rstrip("/")
    return url


def _newer_evidence(candidate: KnowledgeExcerpt, previous: KnowledgeExcerpt) -> bool:
    if candidate.revision_id is not None and previous.revision_id is not None:
        return candidate.revision_id > previous.revision_id or (
            candidate.revision_id == previous.revision_id
            and candidate.authority == "live GSWiki API"
        )
    return candidate.authority == "live GSWiki API"


def _evidence_score(excerpt: KnowledgeExcerpt, terms: tuple[str, ...], character: str) -> int:
    title_score = _title_score(excerpt.title, terms)
    score = title_score + _score(excerpt.text.casefold(), terms)
    score += _phrase_bonus(excerpt.title, terms)
    if excerpt.source.casefold() == f"wiki/characters/{character.casefold()}.md":
        score += _CHARACTER_PAGE_BONUS
        score += _recorded_build_score(excerpt.title, excerpt.text, terms)
    elif excerpt.source.casefold().startswith(f"wiki/characters/{character.casefold()}-"):
        score += _TITLE_TERM_BONUS
        score += _recorded_build_score(excerpt.title, excerpt.text, terms)
    if _redirect_target(excerpt.text) is not None:
        score -= _REDIRECT_PAGE_PENALTY
    elif title_score and excerpt.authority in {"external GSWiki reference", "live GSWiki API"}:
        score += _CANONICAL_PAGE_BONUS
        if re.search(r"\([0-9]+\)$", excerpt.title) and "/" not in excerpt.title:
            score += _CANONICAL_PAGE_BONUS
    if excerpt.authority == "live GSWiki API":
        score += _CANONICAL_PAGE_BONUS
    return score


def _usable_page(text: str, terms: tuple[str, ...]) -> bool:
    # Inspect both raw wikitext and the plain-text prefix retained in old cache
    # entries. Redirects themselves contain no mechanics, even if unresolved.
    if re.match(r"\s*#redirect\b", text, re.IGNORECASE):
        return False
    deprecated = re.search(r"\{\{\s*deprecated\b|^\s*deprecated\b", text, re.IGNORECASE)
    return not deprecated or bool(_HISTORY_QUERY_TERMS.intersection(terms))


def _recorded_build_score(heading: str, text: str, terms: tuple[str, ...]) -> int:
    """Route build follow-ups to recorded numbers, not prior-spell mentions.

    This identifies historical evidence, never establishes that training is
    current or supplies an absent mechanic/formula.
    """
    if not _BUILD_QUERY_TERMS.intersection(terms):
        return 0
    rank = _RANK_FACT.search(text)
    if rank is None or not _BUILD_VOCABULARY.search(text):
        return 0
    score = 80
    if heading.casefold() in {"verified", "skills", "training", "build", "stats"}:
        score += 40
    if rank.start() < 600:
        score += 20
    return score


def _sufficiently_relevant(
    excerpts: Sequence[KnowledgeExcerpt], terms: tuple[str, ...]
) -> bool:
    """Use a small lexical threshold rather than an opaque retrieval score."""

    if not terms:
        return False
    numbers = tuple(term for term in terms if term.isdecimal())
    if numbers:
        # Incidental body mentions do not establish coverage of an explicitly
        # numbered spell. Comparisons need evidence for every requested number.
        return all(any(
            _redirect_target(excerpt.text) is None
            and re.search(rf"(?<![0-9]){re.escape(number)}(?![0-9])", excerpt.title)
            for excerpt in excerpts
        ) for number in numbers)
    meaningful = tuple(term for term in terms if term not in _ONLINE_QUERY_NOISE)
    threshold = 1 if len(meaningful) == 1 else 2
    for excerpt in excerpts:
        text = f"{excerpt.title}\n{excerpt.text}".casefold()
        matches = sum(1 for term in meaningful if term in text)
        if matches >= threshold:
            return True
    return False


def _live_query(terms: tuple[str, ...]) -> str:
    """Prefer an explicit spell number over incidental conversational wording."""

    numeric = [term for term in terms if term.isdecimal()]
    if numeric:
        return " ".join(numeric)
    selected = [term for term in terms if term not in _ONLINE_QUERY_NOISE]
    return " ".join((selected or list(terms))[:6])


def _terms(text: str) -> tuple[str, ...]:
    # Follow-up labels describe the prompt structure, not the requested topic.
    # Keep the actual prior question/answer text available for disambiguation.
    text = _CONTEXT_LABEL.sub("", text)
    return tuple(
        dict.fromkeys(
            word
            for word in _WORD.findall(text.casefold())
            if len(word) > 2 and word not in _STOP_WORDS
        )
    )


def _curated_gameplay_match(heading: str, text: str, terms: tuple[str, ...]) -> bool:
    """Do not fill gameplay evidence with one incidental body mention.

    A topic heading can establish relevance on its own. Otherwise require two
    distinct query terms, not repeated occurrences of the same word. Single-
    term lookups still work; development and recorded-build routing stay separate.
    """
    # Request wording is not a second topic in e.g. "Explain badge".
    meaningful = tuple(term for term in terms if term not in _CURATED_QUERY_NOISE) or terms
    if not meaningful:
        return False
    if _title_score(heading, meaningful):
        return True
    lowered = text.casefold()
    return sum(term in lowered for term in meaningful) >= min(2, len(meaningful))


def _score(text: str, terms: tuple[str, ...]) -> int:
    return sum(min(text.count(term), 4) for term in terms)


def _title_score(title: str, terms: tuple[str, ...]) -> int:
    """Prefer exact title and spell-number matches over incidental body text."""

    lowered = title.casefold()
    score = 0
    for term in terms:
        if term not in lowered:
            continue
        score += _TITLE_TERM_BONUS
        if term.isdecimal() and re.search(rf"(?<![0-9]){re.escape(term)}(?![0-9])", lowered):
            score += _SPELL_NUMBER_TITLE_BONUS
    return score


def _phrase_bonus(text: str, terms: tuple[str, ...]) -> int:
    """Reward literal multi-word query fragments without special-casing a spell."""

    lowered = text.casefold()
    phrases = (
        " ".join(terms[index : index + 2])
        for index in range(len(terms) - 1)
    )
    return sum(_EXACT_PHRASE_BONUS for phrase in phrases if phrase in lowered)


def _redirect_target(wikitext: str) -> str | None:
    match = _REDIRECT.match(wikitext)
    return None if match is None else match.group(1).strip()


def _mirror_diagnostic(
    raw_last_sync: str | None, max_age_hours: float, now: datetime
) -> KnowledgeSourceDiagnostic:
    if raw_last_sync is None:
        return KnowledgeSourceDiagnostic(
            source="local_gswiki",
            status="stale",
            detail="mirror last_sync metadata is missing",
        )
    try:
        last_sync = datetime.fromisoformat(raw_last_sync.replace("Z", "+00:00"))
    except ValueError:
        return KnowledgeSourceDiagnostic(
            source="local_gswiki",
            status="stale",
            detail="mirror last_sync metadata is invalid",
        )
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=UTC)
    age_hours = max(
        0.0,
        (now.astimezone(UTC) - last_sync.astimezone(UTC)).total_seconds() / 3_600,
    )
    if age_hours > max_age_hours:
        return KnowledgeSourceDiagnostic(
            source="local_gswiki",
            status="stale",
            detail=f"mirror is {age_hours:.1f} hours old (threshold {max_age_hours:g})",
        )
    return KnowledgeSourceDiagnostic(
        source="local_gswiki",
        status="success",
        detail=f"mirror age {age_hours:.1f} hours",
    )


def _markdown_chunks(content: str) -> list[tuple[str, str]]:
    chunks: list[tuple[str, str]] = []
    heading = ""
    lines: list[str] = []
    for line in content.splitlines():
        match = _HEADING.match(line)
        if match:
            if any(part.strip() for part in lines):
                chunks.append((heading, "\n".join(lines).strip()))
            heading = match.group(2)
            lines = []
        else:
            lines.append(line)
    if any(part.strip() for part in lines):
        chunks.append((heading, "\n".join(lines).strip()))
    return chunks


def _best_text_window(text: str, terms: tuple[str, ...], size: int = 3_600) -> str:
    if len(text) <= size:
        return text.strip()
    lowered = text.casefold()
    positions = [lowered.find(term) for term in terms]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - size // 4)
    end = min(len(text), start + size)
    start = 0 if start == 0 else text.find("\n", start) + 1
    end_break = text.rfind("\n", start, end)
    if end_break > start:
        end = end_break
    return text[start:end].strip()
