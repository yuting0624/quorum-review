"""Deciding whether two reports describe the same defect.

Needed in two places that look unrelated but are the same question:

- **Across models.** Two models scanning the same diff report one bug twice.
- **Across runs.** A later review re-reports a bug the ledger already knows
  about, and re-posting it is the noise this project exists to avoid.

Exact identity cannot answer either. A finding's ID hashes the code the model
chose to quote, and models do not quote consistently — not between each other,
and not between two runs of the same model on the same input.

Two rules, both learned from live failures:

1. **Position.** Same file, within a couple of lines; further when both quote
   overlapping code. Catches the common case.
2. **Wording.** Same file and strongly overlapping titles. Catches what
   position misses: models anchor the *same* defect at different lines — the
   check or the write for a TOCTOU, the storage or the lookup for an
   unenforced expiry — sometimes fifteen lines apart.

The rules are deliberately asymmetric in their risk. Failing to match produces
a duplicate comment, which is annoying. Matching too eagerly discards a real
finding, which is worse. So the wording rule needs both a high overlap ratio
and an absolute floor on shared words; two short titles sharing one generic
term do not qualify.
"""

from __future__ import annotations

import re
import string
from typing import NamedTuple

_COMMENT = re.compile(r"(#|//).*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")
_WORD = re.compile(r"[a-z_]+")

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

#: Title agreement required to call two reports the same defect regardless of
#: distance: the fraction of the *shorter* title's words that also appear in the
#: longer one, plus an absolute floor on how many words that is.
#:
#: The fraction is measured against the shorter title rather than against the
#: union, because one model routinely says more than the other about the same
#: bug — "Share links never expire: TTL stored as a duration and never checked"
#: against "Expiration check missing when resolving share links". Scoring those
#: over the union punishes the verbose one for being verbose.
#:
#: The floor is what keeps the rule honest. "Missing authorization check on
#: delete" and "Missing authorization check on purge" share three words and are
#: different bugs; requiring four means short titles have to fall back to
#: position. That bias is deliberate — a duplicate comment is a nuisance, a
#: swallowed finding is a failure.
TITLE_SIMILARITY = 0.6
TITLE_MIN_SHARED = 4

#: Words carrying no signal about *which* defect is meant. Without these
#: removed, "check", "in", "the" and friends inflate the overlap between
#: unrelated findings.
_STOPWORDS = frozenset(
    """
    a an and are as at be been between but by can during for from has have in
    into is it its not of on or that the this to when where which with without
    allow allows allowing bug code error issue may might possible potential
    problem should vulnerability vulnerable
    """.split()
)


def normalize_snippet(snippet: str) -> str:
    """Reduce a code snippet to something reformatting will not change.

    Strips comments and collapses all whitespace, so re-indentation, wrapping,
    or an added explanatory comment does not change the result.

    This is not a parser: a ``#`` inside a string literal is treated as the
    start of a comment. That is acceptable because the only requirement is
    determinism — the same snippet must always reduce the same way.
    """
    return _WHITESPACE.sub(" ", _COMMENT.sub("", snippet)).strip()


def _stem(word: str) -> str:
    """Crude stemming: truncate, then drop a trailing plural 's'.

    Stands in for a real stemmer, which is not worth a dependency here — the
    only requirement is that two ways of naming the same thing collide.
    "resolving" and "resolution" both become `resol`; "link" and "links" both
    become `link`.

    Truncating *before* stripping the 's' matters. The other order sends
    "class" to `clas` and "classes" to `class`, so a word would fail to match
    its own plural.
    """
    stem = word[:5]
    return stem[:-1] if len(stem) > 3 and stem.endswith("s") else stem


def title_tokens(title: str) -> set[str]:
    """The content words of a title, stemmed."""
    words = _WORD.findall(title.lower())
    return {_stem(word) for word in words if len(word) > 2 and word not in _STOPWORDS}


class Report(NamedTuple):
    """The minimum needed to compare two reports of a defect.

    A live finding and a stored ledger entry both reduce to this, so neither
    module has to know about the other.
    """

    file_path: str
    line: int
    snippet: str
    title: str


def _same_position(a: Report, b: Report) -> bool:
    distance = abs(a.line - b.line)
    if distance <= LINE_TOLERANCE:
        return True
    if distance > SNIPPET_LINE_TOLERANCE:
        return False

    left = normalize_snippet(a.snippet)
    right = normalize_snippet(b.snippet)
    if not left or not right:
        return False
    return left in right or right in left


def _same_wording(a: Report, b: Report) -> bool:
    left, right = title_tokens(a.title), title_tokens(b.title)
    if not left or not right:
        return False

    shared = left & right
    if len(shared) < TITLE_MIN_SHARED:
        return False
    return len(shared) / min(len(left), len(right)) >= TITLE_SIMILARITY


def same_defect(a: Report, b: Report) -> bool:
    """Whether two reports point at the same defect."""
    if a.file_path != b.file_path:
        return False
    return _same_position(a, b) or _same_wording(a, b)


# -- retiring a finding ----------------------------------------------------

#: Accepted ways to say it. Japanese included because the reviewer can be
#: configured to write its findings in Japanese, and a reply should be able to
#: match the language of the thread it is in.
#:
#: These live here rather than with the handler that acts on them because two
#: callers need to recognise one: `dismissal`, handling the event, and
#: `github_client`, reading a dismissal back out of a thread when the summary
#: comment that recorded it has been deleted.
DISMISSAL_TRIGGERS = (
    "@quorum wontfix",
    "@quorum false positive",
    "@quorum 誤検知",
)

_DISMISSAL_RE = re.compile(
    "|".join(re.escape(trigger) for trigger in DISMISSAL_TRIGGERS), re.IGNORECASE
)


def is_dismissal_text(body: str | None) -> bool:
    """Whether this comment body retires a finding."""
    return bool(_DISMISSAL_RE.search(body or ""))


def without_dismissal_trigger(body: str) -> str:
    """The explanation, with the trigger phrase removed."""
    stripped = _DISMISSAL_RE.sub("", body or "").strip(string.whitespace + ":-—")
    return stripped or "no reason given"
