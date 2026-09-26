"""Offline derived wiki passages; queries never create or repair an index.

Ranges are Python character offsets in the exact versioned normalized snapshot.
Hashes bind those ranges to bytes; they are not authenticity or authority claims.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

from .discovery import _words, source_kind
from .wiki_text import NORMALIZER_VERSION, research_text


SCHEMA_VERSION = 1
PASSAGE_CHARS = 1800
_FOCUSED_PASSAGE_ROWS = 4096
_FOCUSED_DOCUMENT_CHARS = 1_000_000
_HISTORY = frozenset({'history', 'historical', 'deprecated', 'former', 'old', 'obsolete', 'removed', 'replaced'})
_HEADING = re.compile(r'^(#{1,6})[ \t]+(.+?)[ \t]*$', re.MULTILINE)
_TABLE = re.compile(r'^\{\|.*?^\|\}[^\n]*', re.MULTILINE | re.DOTALL)
_REDIRECT = re.compile(r'^\s*#redirect\s*\[\[([^\]|]+)', re.IGNORECASE)


class PassageIndexUnavailable(RuntimeError):
    """Caller should use the legacy mirror path; never migrate while querying."""


@dataclass(frozen=True, slots=True)
class IndexedDocument:
    page_id: int
    title: str
    url: str
    revision_id: int | None
    namespace: int
    normalizer_version: int
    source_fingerprint: str
    document_fingerprint: str
    text: str


@dataclass(frozen=True, slots=True)
class PassageHit:
    page_id: int
    title: str
    url: str
    revision_id: int | None
    namespace: int
    normalizer_version: int
    source_fingerprint: str
    document_fingerprint: str
    start: int
    end: int
    text: str
    text_end: int
    heading_path: tuple[str, ...]
    oversized_structure: bool
    rank: float


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _source_digest(wikitext: str, plain_text: str) -> str:
    return _digest(json.dumps([wikitext, plain_text], ensure_ascii=False, separators=(',', ':')))


def _rows(connection: sqlite3.Connection, sql: str, parameters=()):
    cursor = connection.cursor()
    cursor.row_factory = sqlite3.Row
    return cursor.execute(sql, parameters)


def _check(connection: sqlite3.Connection) -> None:
    try:
        versions = dict(connection.execute('SELECT key, value FROM passage_index_metadata'))
        if versions.get('schema_version') != str(SCHEMA_VERSION) or versions.get('normalizer_version') != str(NORMALIZER_VERSION):
            raise PassageIndexUnavailable('passage index version is outdated')
        if versions.get('complete') != '1':
            raise PassageIndexUnavailable('passage index needs an explicit rebuild')
        for table in ('wiki_documents', 'wiki_documents_fts', 'wiki_passages', 'wiki_passages_fts'):
            connection.execute(f'SELECT * FROM {table} LIMIT 0')
    except sqlite3.Error as error:
        raise PassageIndexUnavailable('passage index is missing or unreadable') from error


def status(connection: sqlite3.Connection) -> dict:
    """Return aggregate coverage without source text or source identifiers."""
    result = {'status': 'missing', 'schema_version': SCHEMA_VERSION,
              'normalizer_version': NORMALIZER_VERSION, 'mirrored_documents': 0,
              'indexed_documents': 0, 'passages': 0, 'oversized_structures': 0,
              'empty_documents': 0, 'excluded_documents': 0, 'namespaces': []}
    try:
        result['mirrored_documents'] = connection.execute('SELECT count(*) FROM pages').fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'passage_index_metadata' not in tables:
            return result
        validity = 'ready'
        try:
            _check(connection)
        except PassageIndexUnavailable as error:
            validity = 'outdated' if 'outdated' in str(error) else 'invalid'
        result['indexed_documents'] = connection.execute('SELECT count(*) FROM wiki_documents').fetchone()[0]
        result['passages'], result['oversized_structures'] = connection.execute(
            'SELECT count(*), coalesce(sum(oversized_structure), 0) FROM wiki_passages').fetchone()
        result['empty_documents'] = connection.execute("SELECT count(*) FROM wiki_documents WHERE text='' ").fetchone()[0]
        result['excluded_documents'] = result['mirrored_documents'] - result['indexed_documents']
        result['namespaces'] = [dict(row) for row in _rows(connection, '''
            SELECT p.namespace, count(*) AS mirrored_documents,
                   count(d.page_id) AS indexed_documents
              FROM pages p LEFT JOIN wiki_documents d USING(page_id)
             GROUP BY p.namespace ORDER BY p.namespace''')]
        result['status'] = validity if result['excluded_documents'] == 0 else 'invalid'
    except sqlite3.Error:
        result['status'] = 'invalid'
    return result


def _schema(connection: sqlite3.Connection) -> None:
    # execute individually: executescript would commit the caller's transaction.
    statements = [
        'CREATE TABLE IF NOT EXISTS passage_index_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
        '''CREATE TABLE IF NOT EXISTS wiki_documents (
            page_id INTEGER PRIMARY KEY, namespace INTEGER NOT NULL, title TEXT NOT NULL,
            url TEXT NOT NULL, revision_id INTEGER, normalizer_version INTEGER NOT NULL,
            source_fingerprint TEXT NOT NULL, document_fingerprint TEXT NOT NULL,
            text TEXT NOT NULL, redirect_target TEXT, is_redirect INTEGER NOT NULL,
            deprecated INTEGER NOT NULL, kind TEXT NOT NULL, passage_count INTEGER NOT NULL)''',
        '''CREATE VIRTUAL TABLE IF NOT EXISTS wiki_documents_fts USING fts5(
            title, content='wiki_documents', content_rowid='page_id', tokenize='porter unicode61')''',
        '''CREATE TABLE IF NOT EXISTS wiki_passages (
            passage_id INTEGER PRIMARY KEY, page_id INTEGER NOT NULL, start INTEGER NOT NULL,
            end INTEGER NOT NULL, heading_path TEXT NOT NULL, body TEXT NOT NULL,
            oversized_structure INTEGER NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS wiki_passages_page ON wiki_passages(page_id, start)',
        '''CREATE VIRTUAL TABLE IF NOT EXISTS wiki_passages_fts USING fts5(
            heading_path, body, content='wiki_passages', content_rowid='passage_id',
            tokenize='porter unicode61')''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_passages_insert AFTER INSERT ON wiki_passages BEGIN
            UPDATE passage_index_metadata SET value='0' WHERE key='complete';
            INSERT INTO wiki_passages_fts(rowid, heading_path, body) VALUES(new.passage_id, new.heading_path, new.body); END''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_passages_delete AFTER DELETE ON wiki_passages BEGIN
            UPDATE passage_index_metadata SET value='0' WHERE key='complete';
            INSERT INTO wiki_passages_fts(wiki_passages_fts, rowid, heading_path, body)
            VALUES('delete', old.passage_id, old.heading_path, old.body); END''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_documents_insert AFTER INSERT ON wiki_documents BEGIN
            UPDATE passage_index_metadata SET value='0' WHERE key='complete';
            INSERT INTO wiki_documents_fts(rowid, title) VALUES(new.page_id, new.title); END''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_documents_delete AFTER DELETE ON wiki_documents BEGIN
            UPDATE passage_index_metadata SET value='0' WHERE key='complete';
            DELETE FROM wiki_passages WHERE page_id=old.page_id;
            INSERT INTO wiki_documents_fts(wiki_documents_fts, rowid, title)
            VALUES('delete', old.page_id, old.title); END''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_pages_insert AFTER INSERT ON pages BEGIN
            UPDATE passage_index_metadata SET value='0' WHERE key='complete'; END''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_pages_delete AFTER DELETE ON pages BEGIN
            DELETE FROM wiki_documents WHERE page_id=old.page_id;
            UPDATE passage_index_metadata SET value='0' WHERE key='complete'; END''',
        '''CREATE TRIGGER IF NOT EXISTS wiki_pages_update AFTER UPDATE ON pages
            WHEN old.wikitext IS NOT new.wikitext OR old.plain_text IS NOT new.plain_text
              OR old.revision_id IS NOT new.revision_id OR old.title IS NOT new.title
              OR old.url IS NOT new.url OR old.namespace IS NOT new.namespace
            BEGIN DELETE FROM wiki_documents WHERE page_id=old.page_id;
            UPDATE passage_index_metadata SET value='0' WHERE key='complete'; END''',
    ]
    for statement in statements:
        connection.execute(statement)


def _drop_derived(connection: sqlite3.Connection) -> None:
    for trigger in ('wiki_pages_insert', 'wiki_pages_delete', 'wiki_pages_update',
                    'wiki_documents_insert', 'wiki_documents_delete',
                    'wiki_passages_insert', 'wiki_passages_delete'):
        connection.execute(f'DROP TRIGGER IF EXISTS {trigger}')
    for table in ('wiki_passages_fts', 'wiki_documents_fts', 'wiki_passages', 'wiki_documents', 'passage_index_metadata'):
        connection.execute(f'DROP TABLE IF EXISTS {table}')


def _blocks(text: str, start: int, end: int):
    """Paragraphs and whole tables, retaining their original character spans."""
    result = []
    cursor = start
    for table in _TABLE.finditer(text, start, end):
        result.extend((cursor + item.start(), cursor + item.end(), False)
                      for item in re.finditer(r'\S[\s\S]*?(?=\n[ \t]*\n|\Z)', text[cursor:table.start()]))
        result.append((table.start(), table.end(), True))
        cursor = table.end()
    result.extend((cursor + item.start(), cursor + item.end(), False)
                  for item in re.finditer(r'\S[\s\S]*?(?=\n[ \t]*\n|\Z)', text[cursor:end]))
    return result


def _chunks(text: str):
    """Keep table-adjacent paragraphs together; never invent repeated table rows."""
    tables = list(_TABLE.finditer(text))
    headings = [heading for heading in _HEADING.finditer(text)
                if not any(table.start() <= heading.start() < table.end() for table in tables)]
    sections = [(0, headings[0].start() if headings else len(text), ())]
    path = []
    for index, heading in enumerate(headings):
        level = len(heading[1])
        path = [(depth, title) for depth, title in path if depth < level]
        path.append((level, heading[2]))
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections.append((heading.start(), end, tuple(title for _, title in path)))
    for start, end, heading_path in sections:
        blocks = _blocks(text, start, end)
        protected = []
        for index, (_, _, table) in enumerate(blocks):
            if table:
                left, right = max(0, index - 1), min(len(blocks) - 1, index + 1)
                if protected and left <= protected[-1][1]:
                    protected[-1] = (protected[-1][0], right)
                else:
                    protected.append((left, right))
        groups = []
        cursor = 0
        for left, right in protected:
            groups.extend(blocks[cursor:left])
            groups.append((blocks[left][0], blocks[right][1], True))
            cursor = right + 1
        groups.extend(blocks[cursor:])
        pending = None
        for left, right, table in groups:
            if table:
                if pending:
                    yield (*pending, heading_path, False)
                    pending = None
                yield (left, right, heading_path, right - left > PASSAGE_CHARS)
                continue
            if pending and right - pending[0] <= PASSAGE_CHARS:
                pending = (pending[0], right)
                continue
            if pending:
                yield (*pending, heading_path, False)
                pending = None
            while right - left > PASSAGE_CHARS:
                stop = text.rfind(' ', left + PASSAGE_CHARS // 2, left + PASSAGE_CHARS)
                stop = stop + 1 if stop >= 0 else left + PASSAGE_CHARS
                yield (left, stop, heading_path, False)
                left = stop
            if left < right:
                pending = (left, right)
        if pending:
            yield (*pending, heading_path, False)


def build_index(connection: sqlite3.Connection) -> dict:
    """Build on a staging connection, reusing unchanged normalized snapshots."""
    connection.execute('SAVEPOINT passage_build')
    normalized = reused = 0
    try:
        try:
            versions = dict(connection.execute('SELECT key, value FROM passage_index_metadata'))
            same_version = (versions.get('schema_version') == str(SCHEMA_VERSION)
                            and versions.get('normalizer_version') == str(NORMALIZER_VERSION))
            if same_version:
                for table in ('wiki_documents', 'wiki_passages', 'wiki_documents_fts', 'wiki_passages_fts'):
                    connection.execute(f'SELECT * FROM {table} LIMIT 0')
        except sqlite3.Error:
            same_version = False
        if not same_version:
            _drop_derived(connection)
        _schema(connection)
        connection.executemany('INSERT OR REPLACE INTO passage_index_metadata VALUES (?, ?)',
            [('schema_version', str(SCHEMA_VERSION)), ('normalizer_version', str(NORMALIZER_VERSION)), ('complete', '0')])
        connection.execute('DELETE FROM wiki_documents WHERE page_id NOT IN (SELECT page_id FROM pages)')
        for page in _rows(connection, 'SELECT * FROM pages ORDER BY page_id'):
            fingerprint = _source_digest(page['wikitext'], page['plain_text'])
            old = _rows(connection, 'SELECT * FROM wiki_documents WHERE page_id=?', (page['page_id'],)).fetchone()
            if (old is not None and old['source_fingerprint'] == fingerprint
                    and old['revision_id'] == page['revision_id']
                    and old['title'] == page['title'] and old['namespace'] == page['namespace']
                    and old['url'] == page['url'] and old['document_fingerprint'] == _digest(old['text'])
                    and old['passage_count'] == connection.execute(
                        'SELECT count(*) FROM wiki_passages WHERE page_id=?', (page['page_id'],)).fetchone()[0]):
                reused += 1
                continue
            connection.execute('DELETE FROM wiki_documents WHERE page_id=?', (page['page_id'],))
            text = research_text(page['wikitext']) or page['plain_text']
            chunks = tuple(_chunks(text))
            redirect = _REDIRECT.match(page['wikitext'])
            deprecated = bool(re.search(r'\{\{\s*deprecated\b|^\s*deprecated\b', page['wikitext'], re.IGNORECASE))
            connection.execute('INSERT INTO wiki_documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (page['page_id'], page['namespace'], page['title'], page['url'], page['revision_id'],
                 NORMALIZER_VERSION, fingerprint, _digest(text), text,
                 redirect[1].strip() if redirect else None,
                 int(bool(re.match(r'\s*#redirect\b', page['wikitext'], re.IGNORECASE))),
                 int(deprecated), source_kind(page['title']), len(chunks)))
            connection.executemany('''INSERT INTO wiki_passages(page_id, start, end, heading_path, body, oversized_structure)
                VALUES (?, ?, ?, ?, ?, ?)''',
                ((page['page_id'], start, end, json.dumps(path, ensure_ascii=False), text[start:end], int(oversized))
                 for start, end, path, oversized in chunks))
            normalized += 1
        # External-content integrity checks verify FTS against stored documents.
        for table in ('wiki_documents_fts', 'wiki_passages_fts'):
            connection.execute(f"INSERT INTO {table}({table}, rank) VALUES('integrity-check', 1)")
        connection.execute("UPDATE passage_index_metadata SET value='1' WHERE key='complete'")
        result = status(connection)
        if result['status'] != 'ready':
            raise PassageIndexUnavailable('derived coverage validation failed')
        connection.execute('RELEASE passage_build')
        return {**result, 'normalized_documents': normalized, 'reused_documents': reused}
    except Exception:
        connection.execute('ROLLBACK TO passage_build')
        connection.execute('RELEASE passage_build')
        raise


def rebuild(database: Path) -> dict:
    """Offline copy, validate, then atomically replace; original survives failure.

    The caller must keep other writers offline for the copy/publication interval.
    Existing readers can finish against the old inode.
    """
    database = Path(database)
    descriptor, name = tempfile.mkstemp(prefix=f'.{database.name}.', suffix='.partial', dir=database.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        source = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            # Mirrors created by sync use DELETE journals. A live WAL cannot be
            # safely paired with a different database inode during publication.
            if source.execute('PRAGMA journal_mode').fetchone()[0] == 'wal':
                raise PassageIndexUnavailable('offline rebuild requires a DELETE-journal mirror; close writers and convert WAL first')
            target = sqlite3.connect(temporary)
            try:
                source.backup(target)
                target.execute('PRAGMA journal_mode=DELETE')
                # Explicit offline rebuild repairs derived data, rather than
                # reusing potentially damaged rows. Ordinary sync still reuses
                # unchanged snapshots through build_index directly.
                _drop_derived(target)
                result = build_index(target)
                target.commit()
                if target.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise PassageIndexUnavailable('replacement database integrity check failed')
            finally:
                target.close()
        finally:
            source.close()
        os.replace(temporary, database)
        return result
    finally:
        temporary.unlink(missing_ok=True)


def load_document(connection: sqlite3.Connection, page_id: int) -> IndexedDocument | None:
    _check(connection)
    try:
        row = _rows(connection, '''SELECT d.*, p.wikitext AS source_wikitext,
              p.plain_text AS source_plain_text, p.revision_id AS source_revision
            FROM wiki_documents d JOIN pages p USING(page_id) WHERE d.page_id=?''', (page_id,)).fetchone()
        if row is None:
            return None
        if (row['normalizer_version'] != NORMALIZER_VERSION
                or row['revision_id'] != row['source_revision']
                or row['document_fingerprint'] != _digest(row['text'])
                or row['source_fingerprint'] != _source_digest(row['source_wikitext'], row['source_plain_text'])):
            raise PassageIndexUnavailable('selected normalized snapshot binding is invalid')
        return IndexedDocument(**{field: row[field] for field in IndexedDocument.__dataclass_fields__})
    except sqlite3.Error as error:
        raise PassageIndexUnavailable('selected normalized snapshot cannot be read') from error


def ranking_passage_bodies(connection: sqlite3.Connection, hits: tuple[PassageHit, ...],
                           *, check=lambda: None) -> tuple[str, ...]:
    """Read only admitted ranges for optional ranking, never entire documents.

    Caller holds a read transaction. Identity/range binding is checked here;
    selected evidence still undergoes load_document's full fingerprint checks.
    Reject the whole attempt if a bound is exceeded or a snapshot changed.
    """
    _check(connection)
    if len(hits) > 36 or sum(hit.end - hit.start for hit in hits) > 200_000:
        raise PassageIndexUnavailable('semantic input exceeds range allowance')
    identity = ('page_id', 'title', 'url', 'namespace', 'revision_id', 'normalizer_version',
                'source_fingerprint', 'document_fingerprint')
    bodies = []
    for hit in hits:
        check()
        if not (0 <= hit.start < hit.end and hit.start <= hit.text_end <= hit.end):
            raise PassageIndexUnavailable('semantic input has invalid range')
        row = _rows(connection, '''SELECT d.page_id, d.title, d.url, d.namespace,
                d.revision_id, d.normalizer_version, d.source_fingerprint,
                d.document_fingerprint, p.heading_path,
                substr(p.body, 1, 200001) AS body, length(p.body) AS size,
                p.body = substr(d.text, p.start + 1, p.end - p.start) AS bound
            FROM wiki_passages p JOIN wiki_documents d USING(page_id)
            WHERE p.page_id=? AND p.start=? AND p.end=? LIMIT 1''',
            (hit.page_id, hit.start, hit.end)).fetchone()
        if (row is None or any(row[key] != getattr(hit, key) for key in identity)
                or hit.normalizer_version != NORMALIZER_VERSION
                or row['size'] != hit.end - hit.start or not row['bound']
                or row['heading_path'] != json.dumps(list(hit.heading_path), ensure_ascii=False)
                or row['body'][:hit.text_end - hit.start] != hit.text):
            raise PassageIndexUnavailable('semantic input snapshot changed')
        bodies.append(row['body'])
    check()
    return tuple(bodies)


def document_passages(connection: sqlite3.Connection, hit: PassageHit, text: str,
                      terms: tuple[str, ...], *, limit: int = 3) -> tuple[PassageHit, ...]:
    """Recover bounded indexed ranges within one already-loaded snapshot.

    The caller holds a read transaction. Only this page's range metadata is read;
    scoring uses its supplied normalized text, not another full-document load or
    global FTS scan. This is lexical selection, not evidence of source authority.
    """
    _check(connection)
    limit = max(0, min(int(limit), 3))
    if not limit or not terms:
        return ()
    if len(text) > _FOCUSED_DOCUMENT_CHARS:
        raise PassageIndexUnavailable('selected page exceeds focused document allowance')
    identity = ('page_id', 'title', 'url', 'namespace', 'revision_id', 'normalizer_version',
                'source_fingerprint', 'document_fingerprint')
    try:
        row = _rows(connection, 'SELECT ' + ', '.join(identity)
                    + ' FROM wiki_documents WHERE page_id=?', (hit.page_id,)).fetchone()
        if (row is None or any(row[key] != getattr(hit, key) for key in identity)
                or hit.normalizer_version != NORMALIZER_VERSION
                or _digest(text) != hit.document_fingerprint):
            raise PassageIndexUnavailable('selected indexed snapshot changed; search again')
        rows = list(_rows(connection, '''SELECT start, end, heading_path, oversized_structure
            FROM wiki_passages WHERE page_id=? ORDER BY start LIMIT ?''',
            (hit.page_id, _FOCUSED_PASSAGE_ROWS + 1)))
        if len(rows) > _FOCUSED_PASSAGE_ROWS:
            raise PassageIndexUnavailable('selected page exceeds focused passage allowance')
        query = _words(' '.join(terms))
        focus = query - _words(hit.title) or query
        candidates = []
        prior_end = 0
        for row in rows:
            start, end = row['start'], row['end']
            if not (prior_end <= start < end <= len(text)):
                raise PassageIndexUnavailable('selected page has invalid indexed ranges')
            prior_end = end
            decoded_path = json.loads(row['heading_path'])
            if (not isinstance(decoded_path, list)
                    or any(not isinstance(heading, str) for heading in decoded_path)):
                raise PassageIndexUnavailable('selected page has invalid heading metadata')
            path = tuple(decoded_path)
            heading_matches = len(focus & _words(' '.join(path)))
            body_matches = len(focus & _words(text[start:end]))
            score = 3 * heading_matches + body_matches
            if not score:
                continue
            preview = text[start:min(end, start + PASSAGE_CHARS)]
            candidate = replace(hit, start=start, end=end, text=preview,
                                text_end=start + len(preview), heading_path=path,
                                oversized_structure=bool(row['oversized_structure']), rank=-float(score))
            candidates.append((score, heading_matches, body_matches, candidate))
        candidates.sort(key=lambda candidate: (-candidate[0], -candidate[1], -candidate[2],
                                               candidate[3].start))
        return tuple(candidate[3] for candidate in candidates[:limit])
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise PassageIndexUnavailable('selected page passage recovery failed') from error


def query_passages(connection: sqlite3.Connection, terms: tuple[str, ...], *,
                   namespaces=(0, 4), limit: int = 36) -> tuple[PassageHit, ...]:
    """Bounded lexical passage/title/noncatalog lanes; load full text only on selection."""
    _check(connection)
    terms = tuple(str(term).replace('"', '""') for term in terms[:10] if str(term).strip())
    namespaces = tuple(dict.fromkeys(int(value) for value in namespaces))
    limit = max(0, min(int(limit), 72))
    if not terms or not namespaces or not limit:
        return ()
    query = ' OR '.join(f'"{term}"*' for term in terms)
    title_query = ' OR '.join(f'"{term}"' for term in terms)
    allowed = ','.join('?' for _ in namespaces)
    history = bool(_HISTORY.intersection(terms))
    fields = '''d.page_id, d.title, d.url, d.revision_id, d.namespace,
        d.normalizer_version, d.source_fingerprint, d.document_fingerprint,
        s.start, s.end, substr(s.body, 1, ?) AS text, s.heading_path, s.oversized_structure'''
    eligible = f'd.namespace IN ({allowed}) AND d.is_redirect=0 AND (? OR d.deprecated=0)'
    try:
        def lane(non_catalog=False):
            kind_filter = "AND d.kind != 'catalog'" if non_catalog else ''
            # The hidden rank column lets FTS supply score-ordered matches and
            # stop joining after LIMIT. Explicit bm25 plus secondary SQL sort
            # instead visits every joined match before applying the row bound.
            # Keep eligibility inside this query so excluded rows cannot use
            # the bounded slots, including on the non-catalog backfill lane.
            return list(_rows(connection, f'''SELECT {fields}, wiki_passages_fts.rank AS rank
                FROM wiki_passages_fts JOIN wiki_passages s ON s.passage_id=wiki_passages_fts.rowid
                JOIN wiki_documents d ON d.page_id=s.page_id
                WHERE wiki_passages_fts MATCH ? AND wiki_passages_fts.rank MATCH 'bm25(3.0, 1.0)' AND {eligible}
                  {kind_filter}
                ORDER BY wiki_passages_fts.rank LIMIT ?''',
                (PASSAGE_CHARS, query, *namespaces, history, limit * 4)))
        general = lane()
        references = [row for row in general if source_kind(row['title']) != 'catalog']
        # The filtered rank stream has the same leading reference matches when
        # the general lane already contains the independent-source allowance.
        # Run a separate stream only when catalogs/repeated ranges crowd it.
        general_counts = Counter(row['page_id'] for row in general)
        if (len({row['page_id'] for row in references}) < 12
                or sum(min(count, 3) for count in general_counts.values()) < limit):
            references = lane(True)
        titles = list(_rows(connection, f'''SELECT d.page_id, d.redirect_target, d.is_redirect
            FROM wiki_documents_fts JOIN wiki_documents d ON d.page_id=wiki_documents_fts.rowid
            WHERE wiki_documents_fts MATCH ? AND d.namespace IN ({allowed})
              AND (? OR d.deprecated=0)
            ORDER BY wiki_documents_fts.rank LIMIT 12''',
            (title_query, *namespaces, history)))
        protected = []
        for title in titles:
            page_id = title['page_id']
            if title['is_redirect']:
                target = connection.execute(f'''SELECT page_id FROM wiki_documents d
                    WHERE d.title=? AND {eligible} LIMIT 1''',
                    (title['redirect_target'], *namespaces, history)).fetchone()
                if target is None:
                    continue
                page_id = target[0]
            best = next((row for row in general + references if row['page_id'] == page_id), None)
            if best is None:
                best = _rows(connection, f'''SELECT {fields}, 0.0 AS rank FROM wiki_passages s
                    JOIN wiki_documents d ON d.page_id=s.page_id WHERE d.page_id=?
                    ORDER BY s.start LIMIT 1''', (PASSAGE_CHARS, page_id)).fetchone()
            if best is not None:
                protected.append(best)
        # Protect one range per independent reference before additional ranges.
        reference_ids = set()
        for row in references:
            if row['page_id'] not in reference_ids:
                protected.append(row)
                reference_ids.add(row['page_id'])
                if len(reference_ids) == 12:
                    break
        selected = []
        seen = set()
        counts = {}
        for row in protected + general + references:
            key = row['page_id'], row['start'], row['end']
            if key in seen or counts.get(row['page_id'], 0) >= 3:
                continue
            seen.add(key)
            counts[row['page_id']] = counts.get(row['page_id'], 0) + 1
            values = dict(row)
            values['heading_path'] = tuple(json.loads(values['heading_path']))
            values['oversized_structure'] = bool(values['oversized_structure'])
            values['text_end'] = values['start'] + len(values['text'])
            if values['start'] < 0 or values['text_end'] > values['end'] or values['normalizer_version'] != NORMALIZER_VERSION:
                raise PassageIndexUnavailable('indexed passage range is invalid')
            selected.append(PassageHit(**values))
            if len(selected) == limit:
                break
        # BM25 discovers ranges globally; its repeated body terms can favor an
        # introduction over the requested child section within the same page.
        # Reorder only the already admitted ranges, preserving every source's
        # slots and the bounded SQL work. Page-title words identify the source,
        # not a section within it. Distinct path matches favor specific children
        # without rewarding repetition; BM25 retains its ordering for ties.
        query_words = _words(' '.join(terms))
        by_source = {}
        for hit in selected:
            by_source.setdefault(hit.page_id, []).append(hit)
        ordered = {}
        for page_id, hits in by_source.items():
            focus = query_words - _words(hits[0].title)
            ordered[page_id] = iter(sorted(hits, key=lambda hit: (
                -len(focus & _words(' '.join(hit.heading_path))), hit.rank)))
        return tuple(next(ordered[hit.page_id]) for hit in selected)
    except (sqlite3.Error, ValueError, TypeError) as error:
        raise PassageIndexUnavailable('passage index query failed') from error
