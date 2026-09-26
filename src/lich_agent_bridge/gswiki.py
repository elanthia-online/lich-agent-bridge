"""Synchronize the public GSWiki text corpus into a local SQLite FTS index."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .settings import Settings


DEFAULT_API_URL = "https://gswiki.play.net/api.php"
DEFAULT_NAMESPACES = (0, 4, 10, 12, 14, 102, 104, 106, 108, 112, 828)
USER_AGENT = "lich-agent-bridge/0.2 (+https://github.com/elanthia-online/lich-agent-bridge)"

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF = re.compile(r"<ref\b[^>]*>.*?</ref\s*>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_INTERNAL_LINK = re.compile(r"\[\[(?:[^\]|]+\|)?([^\]]+)\]\]")
_EXTERNAL_LINK = re.compile(r"\[(?:https?://\S+)(?:\s+([^\]]+))?\]")


@dataclass(frozen=True, slots=True)
class SyncResult:
    pages: int
    namespaces: tuple[int, ...]
    database: Path


def sync(
    database: Path,
    *,
    namespaces: Iterable[int] = DEFAULT_NAMESPACES,
    api_url: str = DEFAULT_API_URL,
    delay: float = 0.05,
    opener: Callable[..., Any] = urlopen,
    progress: Callable[[int, int], None] | None = None,
) -> SyncResult:
    """Mirror current text revisions and atomically prune stale namespace pages."""

    selected = tuple(dict.fromkeys(int(namespace) for namespace in namespaces))
    database = Path(database)
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{database.name}.", suffix=".partial", dir=database.parent
    )
    os.close(descriptor)
    temporary_path = Path(temporary)
    connection: sqlite3.Connection | None = None
    try:
        if database.is_file():
            source = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                target = sqlite3.connect(temporary_path)
                try:
                    source.backup(target)
                finally:
                    target.close()
            finally:
                source.close()
        connection = sqlite3.connect(temporary_path)
        _create_schema(connection)
        sync_id = datetime.now(UTC).isoformat()
        total = 0

        for namespace in selected:
            continuation: dict[str, str] = {}
            namespace_total = 0
            while True:
                parameters = {
                    "action": "query",
                    "generator": "allpages",
                    "gapnamespace": str(namespace),
                    # Revisions with content are limited to 50 pages per response.
                    # Matching the generator batch avoids repeated placeholder pages
                    # across rvcontinue responses and keeps progress truthful.
                    "gaplimit": "50",
                    "prop": "revisions",
                    "rvprop": "ids|timestamp|content",
                    "rvslots": "main",
                    "format": "json",
                    "formatversion": "2",
                    **continuation,
                }
                payload = _request_json(api_url, parameters, opener=opener)
                pages = payload.get("query", {}).get("pages", [])
                revision_pages = [page for page in pages if page.get("revisions")]
                with connection:
                    for page in revision_pages:
                        _upsert_page(connection, page, sync_id)
                namespace_total += len(revision_pages)
                if progress is not None:
                    progress(namespace, namespace_total)
                next_page = payload.get("continue")
                if not isinstance(next_page, dict):
                    break
                continuation = {str(key): str(value) for key, value in next_page.items()}
                if delay:
                    time.sleep(delay)

            # Deletion happens only after a namespace completed successfully.
            with connection:
                connection.execute(
                    "DELETE FROM pages WHERE namespace = ? AND last_seen != ?",
                    (namespace, sync_id),
                )
            total += namespace_total

        with connection:
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('last_sync', ?)",
                (sync_id,),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES('api_url', ?)",
                (api_url,),
            )
        # The same unpublished mirror copy owns the derived ranges. Failed
        # normalization or validation therefore cannot publish a partial index.
        from .passage_index import build_index

        build_index(connection)
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary_path, database)
        return SyncResult(pages=total, namespaces=selected, database=database)
    except Exception:
        if connection is not None:
            connection.close()
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass
        raise


def wikitext_to_text(wikitext: str) -> str:
    """Produce search-friendly text without pretending to be a full renderer."""

    text = _COMMENT.sub(" ", wikitext)
    text = _REF.sub(" ", text)
    text = _INTERNAL_LINK.sub(r"\1", text)
    text = _EXTERNAL_LINK.sub(lambda match: match.group(1) or " ", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    text = text.replace("{{", "\n").replace("}}", "\n")
    text = text.replace("|", "\n").replace("'''", "").replace("''", "")
    text = re.sub(r"^\s*=+\s*(.*?)\s*=+\s*$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _request_json(
    api_url: str,
    parameters: dict[str, str],
    *,
    opener: Callable[..., Any],
) -> dict[str, Any]:
    request = Request(
        f"{api_url}?{urlencode(parameters)}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with opener(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or "error" in payload:
        raise RuntimeError(f"GSWiki returned an invalid response: {payload!r}")
    return payload


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA journal_mode=DELETE;
        CREATE TABLE IF NOT EXISTS pages (
            page_id INTEGER PRIMARY KEY,
            namespace INTEGER NOT NULL,
            title TEXT NOT NULL,
            revision_id INTEGER,
            revision_timestamp TEXT,
            url TEXT NOT NULL,
            wikitext TEXT NOT NULL,
            plain_text TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS pages_namespace_title
            ON pages(namespace, title);
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
            title,
            plain_text,
            content='pages',
            content_rowid='page_id',
            tokenize='porter unicode61'
        );
        CREATE TRIGGER IF NOT EXISTS pages_after_insert AFTER INSERT ON pages BEGIN
            INSERT INTO pages_fts(rowid, title, plain_text)
            VALUES (new.page_id, new.title, new.plain_text);
        END;
        CREATE TRIGGER IF NOT EXISTS pages_after_delete AFTER DELETE ON pages BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, plain_text)
            VALUES ('delete', old.page_id, old.title, old.plain_text);
        END;
        DROP TRIGGER IF EXISTS pages_after_update;
        CREATE TRIGGER pages_after_update AFTER UPDATE ON pages
        WHEN old.title IS NOT new.title OR old.plain_text IS NOT new.plain_text BEGIN
            INSERT INTO pages_fts(pages_fts, rowid, title, plain_text)
            VALUES ('delete', old.page_id, old.title, old.plain_text);
            INSERT INTO pages_fts(rowid, title, plain_text)
            VALUES (new.page_id, new.title, new.plain_text);
        END;
        """
    )


def _upsert_page(
    connection: sqlite3.Connection, page: dict[str, Any], sync_id: str
) -> None:
    revisions = page.get("revisions") or []
    revision = revisions[0] if revisions else {}
    slot = revision.get("slots", {}).get("main", {})
    wikitext = slot.get("content", "")
    title = str(page["title"])
    existing = connection.execute(
        "SELECT wikitext, title, namespace, revision_id FROM pages WHERE page_id = ?",
        (int(page["pageid"]),),
    ).fetchone()
    if existing is not None and tuple(existing) == (
        str(wikitext), title, int(page["ns"]), revision.get("revid")
    ):
        connection.execute(
            "UPDATE pages SET last_seen = ?, revision_timestamp = ? WHERE page_id = ?",
            (sync_id, revision.get("timestamp"), int(page["pageid"])),
        )
        return
    connection.execute(
        """
        INSERT INTO pages(
            page_id, namespace, title, revision_id, revision_timestamp,
            url, wikitext, plain_text, last_seen
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(page_id) DO UPDATE SET
            namespace=excluded.namespace,
            title=excluded.title,
            revision_id=excluded.revision_id,
            revision_timestamp=excluded.revision_timestamp,
            url=excluded.url,
            wikitext=excluded.wikitext,
            plain_text=excluded.plain_text,
            last_seen=excluded.last_seen
        """,
        (
            int(page["pageid"]),
            int(page["ns"]),
            title,
            revision.get("revid"),
            revision.get("timestamp"),
            f"https://gswiki.play.net/{quote(title.replace(' ', '_'), safe='/:()')}",
            str(wikitext),
            wikitext_to_text(str(wikitext)),
            sync_id,
        ),
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument(
        "--config",
        help="settings file (default: LAB_CONFIG or the XDG configuration path)",
    )
    parser.add_argument(
        "--namespace",
        dest="namespaces",
        type=int,
        action="append",
        help="namespace number to mirror; repeat as needed",
    )
    parser.add_argument("--delay", type=float, default=0.05)
    arguments = parser.parse_args(argv)
    settings = Settings.load(path=arguments.config)
    database = arguments.database or settings.knowledge.gswiki_database
    reported: dict[int, int] = {}

    def show_progress(namespace: int, pages: int) -> None:
        previous = reported.get(namespace, 0)
        if pages // 500 > previous // 500:
            print(f"GSWiki namespace {namespace}: {pages} pages", flush=True)
        reported[namespace] = pages

    result = sync(
        database,
        namespaces=arguments.namespaces or DEFAULT_NAMESPACES,
        delay=max(0.0, arguments.delay),
        progress=show_progress,
    )
    print(
        f"GSWiki mirror updated: {result.pages} pages across "
        f"{len(result.namespaces)} namespaces in {result.database}"
    )


if __name__ == "__main__":
    main()
