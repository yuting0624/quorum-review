"""Document reads."""

from . import db
from .config import Config


class NotFound(Exception):
    pass


class Forbidden(Exception):
    pass


def get_document(doc_id: int, user: dict) -> dict:
    rows = db.query("SELECT * FROM documents WHERE id = ?", (doc_id,))
    if not rows:
        raise NotFound(f"document {doc_id}")

    doc = dict(rows[0])
    if doc["owner_id"] != user["id"] and not user["is_admin"]:
        raise Forbidden(f"document {doc_id}")
    return doc


def list_documents(user: dict, limit: int = None, offset: int = 0) -> list:
    """Return one page of the caller's documents.

    ``limit`` is clamped to ``MAX_PAGE_SIZE`` so that a caller passing a huge
    value cannot pull the whole table into memory.
    """
    if limit is None:
        limit = Config.PAGE_SIZE
    limit = min(int(limit), Config.MAX_PAGE_SIZE)

    return [
        dict(row)
        for row in db.query(
            "SELECT id, title, updated_at FROM documents "
            "WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (user["id"], limit, int(offset)),
        )
    ]
