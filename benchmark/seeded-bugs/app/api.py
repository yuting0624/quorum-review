"""The HTTP layer. Every request reaches storage through here.

Validation lives at this boundary rather than in the writers below it, which is
a real and common arrangement: one place to audit, and the writers stay simple.

It is also what makes a diff misleading. A reviewer reading only the changed
lines of a writer sees a parameter joined into a path with nothing guarding it,
and has no way to learn that nothing reaches that function unvalidated.
"""

from __future__ import annotations

from . import auth, reports, validators


def export_report(token: str, doc_id: int, filename: str) -> str:
    """The only route to `reports.write_named_report`.

    The name is checked here, once, before anything downstream touches it.
    """
    user = auth.require_user(token)
    return reports.write_named_report(user, doc_id, validators.export_name(filename))


__all__ = ["export_report"]
