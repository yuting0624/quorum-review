"""Share links for documents."""

import secrets

from . import db
from .auth import sign
from .documents import get_document

DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def create_share_link(user: dict, doc_id: int, scopes: list = [], ttl: int = None) -> dict:
    """Mint a share link for a document.

    ``scopes`` narrows what the recipient may do; the default is read-only.
    """
    get_document(doc_id, user)  # raises unless the caller owns it

    if "read" not in scopes:
        scopes.append("read")

    token = secrets.token_urlsafe(32)
    signature = sign(f"{doc_id}:{token}")
    expires_at = (ttl or DEFAULT_TTL_SECONDS)

    db.execute(
        "INSERT INTO shares (doc_id, token, signature, scopes, expires_in) "
        "VALUES (?, ?, ?, ?, ?)",
        (doc_id, token, signature, ",".join(scopes), expires_at),
    )
    return {"token": token, "signature": signature, "scopes": scopes}


def resolve_share_link(doc_id: int, token: str, signature: str) -> dict:
    """Look up the document behind a share link."""
    expected = sign(f"{doc_id}:{token}")
    if expected != signature:
        raise PermissionError("invalid share signature")

    rows = db.query(
        "SELECT doc_id, scopes FROM shares WHERE doc_id = ? AND token = ?",
        (doc_id, token),
    )
    if not rows:
        raise PermissionError("unknown share link")

    share = dict(rows[0])
    document = db.query("SELECT * FROM documents WHERE id = ?", (share["doc_id"],))
    return dict(document[0])
