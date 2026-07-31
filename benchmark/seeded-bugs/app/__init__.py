"""A minimal document-sharing service.

This exists as a review fixture for measuring detection quality. It is not
intended to be deployed.
"""

from . import admin, auth, documents, export, fetcher, indexer, plugins, search, sharing
from .auth import AuthError
from .documents import Forbidden, NotFound


def handle_get_document(token: str, doc_id: int) -> dict:
    user = auth.require_user(token)
    return documents.get_document(doc_id, user)


def handle_list_documents(token: str, limit=None, offset=0) -> list:
    user = auth.require_user(token)
    return documents.list_documents(user, limit=limit, offset=offset)


def handle_search(token: str, term: str, sort: str = "relevance", limit=None) -> list:
    user = auth.require_user(token)
    return search.search(user, term, sort=sort, limit=limit)


def handle_export(token: str, doc_id: int, filename: str) -> str:
    user = auth.require_user(token)
    return export.export_document(user, doc_id, filename)


def handle_create_share(token: str, doc_id: int, scopes=None) -> dict:
    user = auth.require_user(token)
    if scopes is None:
        return sharing.create_share_link(user, doc_id)
    return sharing.create_share_link(user, doc_id, scopes)


def handle_import_url(token: str, url: str) -> dict:
    user = auth.require_user(token)
    return fetcher.fetch_remote_document(user, url)


def handle_delete_document(token: str, doc_id: int) -> bool:
    user = auth.require_user(token)
    return admin.delete_document(user, doc_id)


def handle_extract_text(token: str, filename: str) -> str:
    user = auth.require_user(token)
    return indexer.extract_text(user, filename)


def handle_available_formats() -> list:
    return plugins.available_formats()


__all__ = [
    "AuthError",
    "Forbidden",
    "NotFound",
    "handle_available_formats",
    "handle_create_share",
    "handle_delete_document",
    "handle_export",
    "handle_extract_text",
    "handle_get_document",
    "handle_import_url",
    "handle_list_documents",
    "handle_search",
]
