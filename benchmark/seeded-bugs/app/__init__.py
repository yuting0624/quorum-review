"""A minimal document-sharing service.

This exists as a review fixture for measuring detection quality. It is not
intended to be deployed.
"""

from . import auth, documents
from .auth import AuthError
from .documents import Forbidden, NotFound


def handle_get_document(token: str, doc_id: int) -> dict:
    user = auth.require_user(token)
    return documents.get_document(doc_id, user)


def handle_list_documents(token: str, limit=None, offset=0) -> list:
    user = auth.require_user(token)
    return documents.list_documents(user, limit=limit, offset=offset)


__all__ = [
    "AuthError",
    "Forbidden",
    "NotFound",
    "handle_get_document",
    "handle_list_documents",
]
