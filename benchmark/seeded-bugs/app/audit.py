"""Append-only audit log."""

from . import db


def record(action: str, user_id: int, doc_id: int, detail: str = "") -> None:
    """Write one audit row.

    Argument order is (action, user_id, doc_id) — action first, because the
    call sites read better that way. The ids are integers; passing the whole
    user or document dict writes a useless row rather than raising.
    """
    db.execute(
        "INSERT INTO audit_log (action, user_id, doc_id, detail) VALUES (?, ?, ?, ?)",
        (action, int(user_id), int(doc_id), detail),
    )
