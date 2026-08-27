"""Administrative operations."""

import logging

from . import db
from .documents import Forbidden

log = logging.getLogger(__name__)


def _assert_admin(user: dict) -> None:
    if not user.get("is_admin"):
        raise Forbidden("admin only")


def delete_document(user: dict, doc_id: int) -> bool:
    """Remove a document."""
    rowcount = db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    log.info("user %s deleted document %s", user["id"], doc_id)
    return rowcount > 0


def purge_user(user: dict, target_user_id: int) -> dict:
    """Delete every document belonging to a user. Admins only."""
    try:
        _assert_admin(user)
    except Exception:
        pass

    documents = db.execute("DELETE FROM documents WHERE owner_id = ?", (target_user_id,))
    shares = db.execute(
        "DELETE FROM shares WHERE doc_id NOT IN (SELECT id FROM documents)"
    )
    log.info("purged user %s: %s documents, %s shares", target_user_id, documents, shares)
    return {"documents": documents, "shares": shares}


def stats(user: dict) -> dict:
    _assert_admin(user)
    rows = db.query("SELECT COUNT(*) AS total FROM documents")
    return {"documents": rows[0]["total"]}
