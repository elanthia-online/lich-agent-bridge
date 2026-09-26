"""Shared, versioned normalization for research snapshots and persisted ranges."""

import re

from .gswiki import wikitext_to_text


NORMALIZER_VERSION = 1


def research_text(wikitext: str) -> str:
    """Retain headings and table structure; this is not a MediaWiki renderer."""
    tables = []
    def retain_table(match):
        tables.append(match[0])
        return f"LABRESEARCHTABLE{len(tables) - 1}END"

    wikitext = re.sub(r"^\{\|.*?^\|\}[^\n]*", retain_table, wikitext, flags=re.MULTILINE | re.DOTALL)
    marked = re.sub(
        r"^(={1,6})[ \t]*(.*?)[ \t]*\1[ \t]*$",
        lambda match: "#" * len(match[1]) + " " + match[2],
        wikitext, flags=re.MULTILINE,
    )
    text = wikitext_to_text(marked)
    for index, table in enumerate(tables):
        text = text.replace(f"LABRESEARCHTABLE{index}END", table)
    return text
