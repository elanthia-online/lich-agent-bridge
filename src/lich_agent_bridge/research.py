"""Question-local discovery and deliberate reading of configured knowledge.

Only this module resolves source locations. Issued handles refer to immutable
document snapshots, not paths, URLs, or model-authored instructions.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from typing import Any

from .knowledge import (
    KnowledgeExcerpt, _CURATED_QUERY_NOISE, _score, _terms, _title_score,
)
from .settings import OnlineFallbackPolicy
from .discovery import rank_discovery, _words
from .passage_index import PassageIndexUnavailable, PASSAGE_CHARS, _chunks


_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_OWNER = re.compile(r"^Character:\s*([A-Za-z][A-Za-z'-]*)\s*$", re.MULTILINE | re.IGNORECASE)
_MAX_DOCUMENT_CHARS = 1_000_000
_MAX_DOCUMENTS = 64


def _handle(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(8)


@dataclass
class _Document:
    source_id: str
    excerpt: KnowledgeExcerpt
    revision: int | str | None
    freshness: str
    scope: str
    sections: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    section_paths: dict[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    passages: dict[str, Any] = field(default_factory=dict)
    snapshot: tuple | None = None
    refresh_attempted: bool = False
    read_started: bool = False
    replacement_id: str | None = None


class ResearchSession:
    """Small source registry and read cursor owner, discarded after one question."""

    def __init__(self, knowledge, *, character: str, max_chars: int = 6000):
        self._knowledge = knowledge
        self._character = character.casefold()
        self._max_chars = max(256, int(max_chars))
        self._documents: dict[str, _Document] = {}
        self._identities: dict[tuple, str] = {}
        self._cursors: dict[str, tuple[str, str | None, int]] = {}
        self._cursor_keys: dict[tuple[str, str | None, int], str] = {}
        self._closed = False
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._documents.clear()
            self._identities.clear()
            self._cursors.clear()
            self._cursor_keys.clear()

    def _check_open(self) -> None:
        if self._closed:
            raise ValueError("research session is closed")

    def validate_read(self, source_id: str, section_id=None, cursor=None) -> None:
        """Lookup-only validation, safe before admitting any batch side effects."""
        self._check_open()
        if not isinstance(source_id, str) or source_id not in self._documents:
            raise ValueError("unknown source handle for this question")
        document = self._documents[source_id]
        if section_id is not None and (
            not isinstance(section_id, str) or section_id not in document.sections
        ):
            raise ValueError("unknown section handle for this source")
        if cursor is not None:
            if not isinstance(cursor, str) or cursor not in self._cursors:
                raise ValueError("unknown read continuation")
            registered = self._cursors[cursor]
            if registered[:2] != (source_id, section_id):
                raise ValueError("read continuation belongs to a different source or section")

    def _register(self, excerpt, *, freshness: str, scope: str, publish=True) -> _Document:
        with self._lock:
            self._check_open()
            return self._register_open(excerpt, freshness=freshness, scope=scope, publish=publish)

    def _register_open(self, excerpt, *, freshness: str, scope: str, publish=True) -> _Document:
        revision = excerpt.revision_id
        if revision is None:
            revision = "sha256:" + hashlib.sha256(excerpt.text.encode()).hexdigest()
        identity = self._identity(scope, excerpt, revision)
        if identity in self._identities:
            return self._documents[self._identities[identity]]
        if len(self._documents) >= _MAX_DOCUMENTS or len(excerpt.text) > _MAX_DOCUMENT_CHARS:
            raise ValueError("question source storage limit reached")
        document = _Document(_handle("src"), excerpt, revision, freshness, scope)
        headings = list(_HEADING.finditer(excerpt.text))
        if headings and headings[0].start() > 0:
            section_id = _handle("sec")
            document.sections[section_id] = ("Introduction", 0, headings[0].start())
            document.section_paths[section_id] = ((0, 'introduction'),)
        ancestors: list[tuple[int, str]] = []
        for index, heading in enumerate(headings):
            # A selected heading includes its child headings, preserving nearby
            # table explanations and qualifications instead of sentence snippets.
            end = len(excerpt.text)
            for following in headings[index + 1:]:
                if len(following[1]) <= len(heading[1]):
                    end = following.start()
                    break
            level = len(heading[1])
            while ancestors and ancestors[-1][0] >= level:
                ancestors.pop()
            ancestors.append((level, ' '.join(heading[2].split()).casefold()))
            section_id = _handle("sec")
            document.sections[section_id] = (heading[2], heading.start(), end)
            document.section_paths[section_id] = tuple(ancestors)
        if not document.sections:
            section_id = _handle("sec")
            document.sections[section_id] = (excerpt.title, 0, len(excerpt.text))
            document.section_paths[section_id] = ((0, 'whole document'),)
        if publish:
            self._documents[document.source_id] = document
            self._identities[identity] = document.source_id
        return document

    @staticmethod
    def _identity(scope, excerpt, revision):
        # A revision alone does not identify offsets produced by a normalizer.
        return (scope, excerpt.source, revision, hashlib.sha256(excerpt.text.encode()).hexdigest())

    def _register_passages(self, document, hits):
        """Issue range handles without inventing structural heading identity."""
        selected = []
        fingerprint = hashlib.sha256(document.excerpt.text.encode()).hexdigest() if hits else None
        for hit in hits:
            snapshot = (hit.page_id, hit.revision_id, hit.normalizer_version,
                        hit.source_fingerprint, hit.document_fingerprint)
            if document.snapshot is not None and document.snapshot != snapshot:
                raise PassageIndexUnavailable('passage belongs to a different normalized snapshot')
            if not (0 <= hit.start < hit.end <= len(document.excerpt.text)):
                raise PassageIndexUnavailable('passage range is outside its normalized snapshot')
            if fingerprint != hit.document_fingerprint or hit.text != document.excerpt.text[hit.start:hit.text_end]:
                raise PassageIndexUnavailable('passage text fingerprint does not match its document')
            document.snapshot = snapshot
            key = next((key for key, prior in document.passages.items()
                        if (prior.start, prior.end) == (hit.start, hit.end)), None)
            if key is None:
                key = _handle('sec')
                document.passages[key] = hit
                document.sections[key] = (' / '.join(hit.heading_path) or document.excerpt.title,
                                          hit.start, hit.end)
            selected.append(key)
        return selected

    @staticmethod
    def _provenance(document: _Document) -> dict[str, Any]:
        excerpt = document.excerpt
        provenance = {"source": excerpt.source, "authority": excerpt.authority,
                "url": excerpt.url, "revision_id": document.revision,
                "retrieved_at": excerpt.retrieved_at, "freshness": document.freshness}
        if document.snapshot is not None:
            provenance.update(page_id=document.snapshot[0], normalizer_version=document.snapshot[2],
                              text_fingerprint=document.snapshot[4])
        return provenance

    def _candidate(self, document, terms=(), passage_ids=()) -> dict[str, Any]:
        text = document.excerpt.text
        positions = [text.casefold().find(term) for term in terms if term in text.casefold()]
        start = max(0, min(positions, default=0) - 80)
        outline = sorted(((key, value) for key, value in document.sections.items()
                          if key not in document.passages), key=lambda item: -_title_score(item[1][0], terms))
        passages = []
        for key in passage_ids:
            hit = document.passages[key]
            preview_positions = [hit.text.casefold().find(term) for term in terms if term in hit.text.casefold()]
            preview_start = max(0, min(preview_positions, default=0) - 80)
            passages.append({'section_id': key, 'kind': 'passage',
                             'title': document.sections[key][0], 'heading_path': list(hit.heading_path),
                             'start': hit.start, 'end': hit.end,
                             'snippet': hit.text[preview_start:preview_start + 240],
                             'oversized_structure': hit.oversized_structure})
        sections = passages + [{"section_id": key, "kind": "section", "title": value[0],
                                "start": value[1], "end": value[2]} for key, value in outline]
        return {"source_id": document.source_id, "title": document.excerpt.title,
                "snippet": passages[0]['snippet'] if passages else text[start:start + 240],
                "provenance": self._provenance(document), "sections": sections[:16],
                "outline_complete": len(sections) <= 16, "evidence_kind": "discovery"}

    @staticmethod
    def _source(document, evidence_kind: str, **fields) -> dict[str, Any]:
        source = ResearchSession._provenance(document)
        source.update(source_id=document.source_id, title=document.excerpt.title,
                      evidence_kind=evidence_kind, **fields)
        source["authority"] += " — " + ("discovery snippet" if evidence_kind == "discovery" else "read passage")
        return source

    def _pack_discovery(self, prepared, terms):
        """Reserve a useful read per source before spending on more outline."""
        items, sources, selected = [], [], []
        remaining = []
        omitted = 0
        previews = {}
        for _, item, _ in prepared:
            previews[item['source_id']] = item['snippet']
            if item['sections'] and item['sections'][0].get('snippet') == item['snippet']:
                # The candidate already previews this exact matched range.
                item['sections'][0].pop('snippet')
        minimum = {'items': [{**item, 'sections': item['sections'][:1], 'outline_complete': False}
                             for _, item, _ in prepared],
                   'sources': [source for _, _, source in prepared]}
        if len(json.dumps(minimum)) > self._max_chars:
            for _, item, _ in prepared:
                snippet = item['snippet']
                if len(snippet) <= 80:
                    continue
                positions = [snippet.casefold().find(term) for term in terms if term in snippet.casefold()]
                start = max(0, min(positions, default=0) - 20)
                end = min(len(snippet), start + 78)
                item['snippet'] = ('…' if start else '') + snippet[start:end] + ('…' if end < len(snippet) else '')

        def fits():
            return len(json.dumps({"items": items, "sources": sources})) <= self._max_chars

        for document, item, source in prepared:
            sections = item['sections']
            complete = item['outline_complete']
            # Indexed candidates put their strongest matched passage first;
            # legacy candidates put their strongest structural section first.
            item['sections'] = sections[:1]
            item['outline_complete'] = complete and len(sections) <= 1
            items.append(item)
            sources.append(source)
            if not fits():
                items.pop()
                sources.pop()
                omitted += 1
                continue
            selected.append(document)
            extras = sections[1:]
            # Keep a full-section route alongside the narrow matched passage.
            structural = next((section for section in extras if section['kind'] == 'section'), None)
            if structural is not None:
                extras = [structural] + [section for section in extras if section is not structural]
            remaining.append((item, extras, complete, sections))

        # Each source gets one opportunity per round. A large heading or
        # passage cannot prevent a smaller remaining handle from fitting.
        for index in range(max((len(extras) for _, extras, _, _ in remaining), default=0)):
            for item, extras, _, _ in remaining:
                if index >= len(extras):
                    continue
                item['sections'].append(extras[index])
                if not fits():
                    item['sections'].pop()
        for item, _, complete, sections in remaining:
            included = {section['section_id'] for section in item['sections']}
            # Preserve passage-first presentation and relevance order after
            # allocation; allocation priority is not a new relevance score.
            item['sections'] = [section for section in sections if section['section_id'] in included]
            item['outline_complete'] = complete and len(included) == len(sections)
        # Restore longer previews when the selected handles leave enough room.
        for item in items:
            compact = item['snippet']
            item['snippet'] = previews[item['source_id']]
            if not fits():
                item['snippet'] = compact
        return items, sources, selected, omitted

    def search(self, query: str, scope: str = "reference") -> dict[str, Any]:
        return self._search(query, scope)

    def _coverage_window(self, document, hits, terms):
        """Cover complementary query terms in one bounded, contiguous read.

        This is lexical coverage, not a semantic completeness judgement. Never
        concatenate disjoint source text: intervening qualifiers remain visible.
        Existing passage endpoints preserve protected tables and snapshot identity.
        """
        if len(hits) < 2:
            return hits
        focus = _words(' '.join(terms)) - _words(document.excerpt.title)
        text = document.excerpt.text
        first = hits[0]
        start, end = first.start, first.end
        covered = focus & _words(text[start:end])
        remaining = list(hits[1:])
        paths = [first.heading_path]
        oversized = first.oversized_structure
        allowance = max(128, self._max_chars - 1000)
        while remaining:
            choices = []
            for position, hit in enumerate(remaining):
                left, right = min(start, hit.start), max(end, hit.end)
                added = (focus & _words(text[hit.start:hit.end])) - covered
                if added and right - left <= allowance:
                    choices.append((len(added), -(right-left), -position, hit))
            if not choices:
                break
            hit = max(choices, key=lambda choice: choice[:3])[3]
            remaining.remove(hit)
            start, end = min(start, hit.start), max(end, hit.end)
            covered |= focus & _words(text[start:end])
            paths.append(hit.heading_path)
            oversized = oversized or hit.oversized_structure
        if (start, end) == (first.start, first.end):
            return hits
        # A common ancestor is real heading context; unrelated leaf headings
        # must not be presented as if one were nested beneath the other.
        common = []
        for levels in zip(*paths):
            if len(set(levels)) != 1:
                break
            common.append(levels[0])
        preview = text[start:min(end, start + PASSAGE_CHARS)]
        # The contiguous window can also include an unselected protected table
        # between its endpoints. Reuse the indexer's structure rules on this
        # read-sized window so its warning is not lost during composition.
        oversized = oversized or any(chunk[3] for chunk in _chunks(text[start:end]))
        window = replace(first, start=start, end=end, text=preview,
                         text_end=start+len(preview), heading_path=tuple(common),
                         oversized_structure=oversized)
        return (window, *(hit for hit in hits
                          if not (start <= hit.start and hit.end <= end)))

    def _search(self, query, scope, *, legacy_reason=None):
        self._check_open()
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("query must be nonempty and at most 2000 characters")
        if scope not in {"reference", "character", "development"}:
            raise ValueError("unknown knowledge scope")
        terms = tuple(term for term in _terms(query) if term not in _CURATED_QUERY_NOISE) or _terms(query)
        candidates, diagnostics = self._markdown(terms, scope)
        passage_hits = {}
        if scope == "reference":
            if legacy_reason is None:
                local, diagnostic, passage_hits = self._knowledge._research_gswiki(terms=terms)
            else:
                local, diagnostic = self._knowledge._search_gswiki(terms=terms, full_documents=True)
                diagnostics.append({'source': 'passage_index', 'status': 'unavailable',
                                    'detail': 'Using legacy page search: ' + legacy_reason[:160]})
            diagnostics.append(diagnostic.to_mapping())
            candidates.extend((item, "stale" if diagnostic.status == "stale" else "mirror_snapshot", scope) for item in local)
            # Stale discovery is still useful. Refresh only the page selected for
            # reading, not another broad query yielding unrelated replacements.
            if not local:
                live, live_status = self._knowledge._search_live_gswiki_if_needed(
                    question=query, terms=terms, local_excerpts=local, local_diagnostic=diagnostic)
                diagnostics.append(live_status.to_mapping())
                candidates.extend((item, "verified_at_retrieval", scope) for item in live)
                web, web_status = self._knowledge._search_general_web_if_needed(
                    question=query, terms=terms, local_excerpts=local,
                    local_diagnostic=diagnostic, live_excerpts=live, live_diagnostic=live_status)
                diagnostics.append(web_status.to_mapping())
                # General web is discovery-only: it cannot grant URL read access.
                candidates.extend((item, "snippet_only", scope) for item in web)
        self._check_open()
        ranked = rank_discovery(candidates, terms)
        ranked, semantic_status = self._knowledge._rerank_research(
            ranked, passage_hits, query=query, terms=terms, check=self._check_open)
        if semantic_status is not None:
            diagnostics.append(semantic_status)
        prepared = []
        omitted = 0
        indexed_loads = 0
        available_documents = _MAX_DOCUMENTS - len(self._documents)
        for candidate in ranked:
            if len(prepared) >= 6:
                omitted += 1
                continue
            excerpt, freshness, candidate_scope = candidate.excerpt, candidate.freshness, candidate.scope
            hits = passage_hits.get((excerpt.source, excerpt.revision_id), ()) if candidate_scope == 'reference' else ()
            try:
                if hits:
                    if indexed_loads >= 6:
                        omitted += 1
                        continue
                    indexed_loads += 1
                    excerpt = self._knowledge._load_research_document(hits[0], retrieved_at=excerpt.retrieved_at)
                document = self._register(excerpt, freshness=freshness, scope=candidate_scope, publish=False)
                passage_ids = self._register_passages(document, hits)
                if hits:
                    focused = self._knowledge._research_document_passages(hits[0], excerpt, terms=terms)
                    if focused:
                        passage_ids = self._register_passages(document,
                            self._coverage_window(document, focused, terms))
            except (PassageIndexUnavailable, sqlite3.Error) as error:
                # Concurrent replacement or selected-row corruption invalidates
                # derived evidence. Restart once from the usable page mirror.
                return self._search(query, scope, legacy_reason=str(error))
            except ValueError:
                omitted += 1
                continue
            if any(prior.source_id == document.source_id for prior, _, _ in prepared):
                continue
            if document.source_id not in self._documents:
                if available_documents <= 0:
                    omitted += 1
                    continue
                available_documents -= 1
            item = self._candidate(document, terms, passage_ids)
            item['ranking'] = candidate.explanation()
            source = self._source(document, "discovery")
            prepared.append((document, item, source))
        items, sources, selected, budget_omitted = self._pack_discovery(prepared, terms)
        omitted += budget_omitted
        for document in selected:
            with self._lock:
                self._check_open()
                self._documents[document.source_id] = document
                self._identities[self._identity(document.scope, document.excerpt, document.revision)] = document.source_id
        if omitted:
            diagnostics.append({"source": "knowledge.search", "status": "partial", "detail": f"{omitted} candidates omitted; refine search to discover them"})
        self._check_open()
        return {"status": "partial" if omitted else ("succeeded" if items else "not_found"),
                "data": {"items": items, "scope": scope}, "sources": sources, "diagnostics": diagnostics}

    def read(self, source_id: str, section_id=None, cursor=None) -> dict[str, Any]:
        self.validate_read(source_id, section_id, cursor)
        document = self._documents[source_id]
        diagnostics = []
        if document.replacement_id is not None:
            return self._read_replacement(document, self._documents[document.replacement_id],
                                          section_id, cursor, diagnostics)
        if document.freshness == "snippet_only":
            return {"status": "unavailable", "data": {"source_id": source_id, "reason": "Only a search snippet is available; arbitrary web page reading is not enabled."}, "sources": [], "diagnostics": []}
        if document.freshness == "stale" and not document.refresh_attempted and not document.read_started:
            document.refresh_attempted = True
            knowledge = self._knowledge
            if knowledge._online_fallback is not OnlineFallbackPolicy.DISABLED and knowledge._live_gswiki is not None:
                fresh, diagnostic = knowledge._live_gswiki.read(document.excerpt.title)
                self._check_open()
                diagnostics.append(diagnostic.to_mapping())
                if fresh is not None:
                    if fresh.revision_id != document.revision:
                        replacement = self._register(fresh, freshness="verified_at_retrieval", scope=document.scope)
                        document.replacement_id = replacement.source_id
                        replacement.freshness = 'verified_at_retrieval'
                        replacement.excerpt = replace(replacement.excerpt, retrieved_at=fresh.retrieved_at)
                        return self._read_replacement(document, replacement, section_id, cursor, diagnostics)
                    document.freshness = "verified_at_retrieval"
                    document.excerpt = replace(document.excerpt, retrieved_at=fresh.retrieved_at)
            else:
                diagnostics.append({"source": "live_gswiki", "status": "disabled", "detail": "Selected mirror page remains unverified stale; online refresh is disabled."})
        return self._read_passage(document, section_id, cursor, diagnostics)

    def _read_replacement(self, previous, replacement, section_id, cursor, diagnostics):
        """Complete the same read only when its target survives unambiguously.

        A cursor is an offset into one immutable snapshot and is never remapped.
        Section identity includes heading levels and ancestors, not just a leaf
        label that may occur under an unrelated topic in the replacement.
        """
        self._check_open()
        if cursor is not None:
            return self._revision_changed(previous, replacement, diagnostics)
        selected_section = None
        if section_id is not None:
            if section_id in previous.passages:
                # A chunk is a fingerprint-bound range, not a heading. Even a
                # uniquely matching parent cannot safely reinterpret its offsets.
                return self._revision_changed(previous, replacement, diagnostics)
            path = previous.section_paths[section_id]
            matches = [key for key, value in replacement.section_paths.items() if value == path]
            if list(previous.section_paths.values()).count(path) != 1 or len(matches) != 1:
                return self._revision_changed(previous, replacement, diagnostics)
            selected_section = matches[0]
        result = self._read_passage(replacement, selected_section, None, diagnostics)
        result['data']['refreshed_from'] = {'source_id': previous.source_id,
                                          'revision_id': previous.revision}
        result['diagnostics'].append({'source': 'knowledge.read', 'status': 'refreshed_read',
                                      'detail': 'Selected page changed revision; the requested passage was read from its replacement in this call.'})
        return result

    def _read_passage(self, document, section_id, cursor, diagnostics):
        self._check_open()
        source_id = document.source_id
        document.read_started = True
        start, limit = (0, len(document.excerpt.text)) if section_id is None else document.sections[section_id][1:]
        if cursor is not None:
            start = self._cursors[cursor][2]
        end = min(limit, start + max(128, self._max_chars - 1000))
        next_cursor = None
        if end < limit:
            with self._lock:
                self._check_open()
                key = (source_id, section_id, end)
                next_cursor = self._cursor_keys.setdefault(key, _handle("cur"))
                self._cursors[next_cursor] = key
        range_start = 0 if section_id is None else document.sections[section_id][1]
        source = self._source(document, "read", section_id=section_id, start=start, end=end,
                              complete=start == range_start and end == limit,
                              kind='passage' if section_id in document.passages else 'section' if section_id else 'document')
        self._check_open()
        return {"status": "succeeded", "data": {"source_id": source_id,
                "title": document.excerpt.title, "section_id": section_id,
                "kind": source['kind'],
                "text": document.excerpt.text[start:end], "start": start, "end": end,
                "complete": start == range_start and end == limit, "at_end": end == limit,
                "next_cursor": next_cursor,
                "provenance": self._provenance(document)}, "sources": [source], "diagnostics": diagnostics}

    def _revision_changed(self, previous, replacement, diagnostics):
        return {"status": "revision_changed", "data": {"source_id": previous.source_id,
                "reason": "Selected source changed revision; select a section from the replacement to avoid mixing versions.",
                "replacement": self._candidate(replacement)}, "sources": [], "diagnostics": diagnostics}

    def _markdown(self, terms, scope):
        root = self._knowledge._wiki_root.resolve()
        if not root.is_dir():
            return [], [{"source": "curated_wiki", "status": "missing", "detail": "Configured Markdown root is unavailable"}]
        records = []
        identities = {self._character} if self._character else set()
        for path in root.rglob("*.md"):
            try:
                resolved = path.resolve()
                if not resolved.is_relative_to(root) or path.stat().st_size > _MAX_DOCUMENT_CHARS:
                    continue
                relative = path.relative_to(root)
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            owner = _OWNER.search(content)
            if owner:
                identities.add(owner[1].casefold())
            if relative.parts[0].casefold() == "characters":
                identities.add(path.stem.split("-", 1)[0].casefold())
            records.append((relative, content, owner[1].casefold() if owner else None))
        candidates = []
        for relative, content, owner in records:
            path = relative.as_posix()
            first = relative.parts[0].casefold()
            is_character = first == "characters"
            if is_character:
                owner = relative.stem.split("-", 1)[0].casefold()
            mentions = {name for name in identities if re.search(r"(?<![\w'-])" + re.escape(name) + r"(?![\w'-])", content, re.IGNORECASE)}
            # Fail closed for mixed-character notes even outside characters/.
            # Unknown free-prose identities cannot be inferred reliably: owners
            # should use characters/<name>.md or a Character: <name> metadata line.
            if scope == "character":
                if owner != self._character or mentions - {self._character}:
                    continue
            elif owner or mentions or is_character:
                continue
            elif scope == "reference" and first in {"project", "lich"}:
                continue
            elif scope == "development" and first not in {"project", "lich"}:
                continue
            title_match = _HEADING.search(content)
            title = title_match[2] if title_match else relative.stem
            if _score(content.casefold(), terms) + _title_score(title, terms) <= 0:
                continue
            candidates.append((KnowledgeExcerpt("recorded character notes" if scope == "character" else "configured Markdown reference",
                                                title, content, f"wiki/{path}"), "historical_record" if scope == "character" else "local_snapshot", scope))
        return candidates, [{"source": "curated_wiki", "status": "success" if candidates else "empty", "detail": f"{len(candidates)} {scope} documents matched"}]
