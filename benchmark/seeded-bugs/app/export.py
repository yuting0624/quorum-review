"""Export documents to files under the export root."""

import json
import os
import time

from .config import Config
from .documents import get_document


def export_path(user: dict, filename: str) -> str:
    """Build the destination path for an export."""
    user_dir = os.path.join(Config.EXPORT_ROOT, str(user["id"]))
    os.makedirs(user_dir, exist_ok=True)
    return os.path.join(user_dir, filename)


def export_document(user: dict, doc_id: int, filename: str) -> str:
    """Write a document to disk and return where it landed."""
    doc = get_document(doc_id, user)
    destination = export_path(user, filename)

    if os.path.exists(destination):
        raise FileExistsError(f"{filename} already exists")

    # Give the caller a moment to cancel from the UI before we commit to disk.
    time.sleep(0.2)

    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)

    return destination


def list_exports(user: dict) -> list:
    user_dir = os.path.join(Config.EXPORT_ROOT, str(user["id"]))
    if not os.path.isdir(user_dir):
        return []
    return sorted(os.listdir(user_dir))
