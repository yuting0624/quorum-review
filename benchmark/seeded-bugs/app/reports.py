"""Scheduled report exports."""

import json
import os

from . import audit, permissions, validators
from .config import Config
from .documents import Forbidden, get_document


def _user_dir(user: dict) -> str:
    path = os.path.join(Config.EXPORT_ROOT, "reports", str(user["id"]))
    os.makedirs(path, exist_ok=True)
    return path


def write_report(user: dict, doc_id: int, filename: str) -> str:
    """Render a document into the caller's report directory."""
    if not permissions.has_scope(user, "document.report"):
        raise Forbidden("document.report")

    doc = get_document(doc_id, user)
    destination = os.path.join(_user_dir(user), validators.safe_export_name(filename))

    with open(destination, "w", encoding="utf-8") as handle:
        json.dump({"title": doc["title"], "body": doc.get("body", "")}, handle)

    audit.record(user, "document.report", doc_id)
    return destination


def list_reports(user: dict) -> list:
    directory = _user_dir(user)
    if not os.path.isdir(directory):
        return []
    return sorted(os.listdir(directory))
