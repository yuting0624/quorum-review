"""Deciding whether two reports describe the same defect.

Needed in two places that look unrelated but are the same question:

- **Across models.** Two models scanning the same diff report one bug twice.
- **Across runs.** A later review re-reports a bug the ledger already knows
  about, and re-posting it is the noise this project exists to avoid.

Exact identity cannot answer either. A finding's ID hashes the code the model
chose to quote, and models do not quote consistently — not between each other,
and not between two runs of the same model on the same input. That is why the
first attempt at ledger suppression failed: a bug reported twice produced two
different IDs, and eight comments were posted a second time.

So matching is positional, with quoted code allowed to widen the window.
"""

from __future__ import annotations

import re

_COMMENT = re.compile(r"(#|//).*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")

#: How far apart two reports of the same defect may sit on position alone.
#: Models routinely disagree by a line about where a problem "is" — the
#: assignment or the call that uses it — but two genuinely distinct bugs almost
#: never land this close.
LINE_TOLERANCE = 2

#: The outer limit even when both reports quote overlapping code. Identical
#: snippets are not proof of identity: a file can contain the same
#: `except Exception: pass` twice, and merging two of those would discard a real
#: finding. Beyond this distance, treat them as separate.
SNIPPET_LINE_TOLERANCE = 15


def normalize_snippet(snippet: str) -> str:
    """Reduce a code snippet to something reformatting will not change.

    Strips comments and collapses all whitespace, so re-indentation, wrapping,
    or an added explanatory comment does not change the result.

    This is not a parser: a ``#`` inside a string literal is treated as the
    start of a comment. That is acceptable because the only requirement is
    determinism — the same snippet must always reduce the same way.
    """
    return _WHITESPACE.sub(" ", _COMMENT.sub("", snippet)).strip()


def same_defect(
    file_a: str,
    line_a: int,
    snippet_a: str,
    file_b: str,
    line_b: int,
    snippet_b: str,
) -> bool:
    """Whether two reports point at the same defect.

    Takes primitives rather than objects so that a live finding and a stored
    ledger entry can be compared without either module depending on the other.
    """
    if file_a != file_b:
        return False

    distance = abs(line_a - line_b)
    if distance <= LINE_TOLERANCE:
        return True
    if distance > SNIPPET_LINE_TOLERANCE:
        return False

    left = normalize_snippet(snippet_a)
    right = normalize_snippet(snippet_b)
    if not left or not right:
        return False
    return left in right or right in left
