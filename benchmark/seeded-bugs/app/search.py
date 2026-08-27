"""Full-text search over documents."""

from . import db
from .config import Config

SORTABLE = {"relevance": "rank", "recent": "updated_at", "title": "title"}


def search(user: dict, term: str, sort: str = "relevance", limit: int = None) -> list:
    """Search the caller's documents for a term."""
    if limit is None:
        limit = Config.PAGE_SIZE
    column = SORTABLE.get(sort, "rank")

    sql = f"""
        SELECT id, title, snippet(documents_fts) AS rank, updated_at
        FROM documents_fts
        WHERE owner_id = {user["id"]} AND documents_fts MATCH '{term}'
        ORDER BY {column} DESC
        LIMIT {int(limit)}
    """
    return [dict(row) for row in db.query(sql)]


def suggest(user: dict, prefix: str, limit: int = 10) -> list:
    """Autocomplete for the search box."""
    rows = db.query(
        "SELECT DISTINCT title FROM documents "
        "WHERE owner_id = ? AND title LIKE ? ORDER BY title LIMIT ?",
        (user["id"], f"{prefix}%", int(limit)),
    )
    return [row["title"] for row in rows]
