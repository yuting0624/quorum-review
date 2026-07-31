"""Letting a human retire a finding the models got wrong.

Without this, a false positive is permanent furniture: the ledger suppresses it
from being re-posted, so it never goes away and never gets acknowledged either.
The reviewer ends up with an open comment nobody can close and no way to say so.

Replying to the comment with ``@quorum wontfix`` marks the finding dismissed.
It is not re-reported afterwards, and the reason is kept — the intent is that a
later phase feeds accumulated reasons back into the review criteria, so the same
mistake stops being made rather than merely being silenced.

Authorisation is enforced in the workflow, not here: the `if:` condition
requires the commenter to be an owner, member, or collaborator. A dismissal
posted by anyone else never reaches this code.
"""

from __future__ import annotations

import re
from typing import Any

from .github_client import GitHubClient
from .ledger import LedgerEntry, replace_marker

#: Accepted ways to say it. Japanese included because the reviewer can be
#: configured to write its findings in Japanese, and a reply should be able to
#: match the language of the thread it is in.
TRIGGERS = (
    "@quorum wontfix",
    "@quorum false positive",
    "@quorum 誤検知",
)

_TRIGGER_RE = re.compile(
    "|".join(re.escape(trigger) for trigger in TRIGGERS), re.IGNORECASE
)


def is_dismissal(event: dict[str, Any]) -> bool:
    """Whether this event is someone retiring a finding.

    Requires a reply *within a thread*: ``in_reply_to_id`` is how the finding
    being dismissed is identified. A new top-level comment saying the same words
    names nothing and is ignored.
    """
    comment = event.get("comment")
    if not isinstance(comment, dict) or not comment.get("in_reply_to_id"):
        return False
    return bool(_TRIGGER_RE.search(comment.get("body") or ""))


def extract_reason(body: str) -> str:
    """The explanation, with the trigger phrase removed."""
    return _TRIGGER_RE.sub("", body or "").strip(" \t\r\n:-—") or "no reason given"


async def handle(github: GitHubClient, event: dict[str, Any], number: int) -> int:
    """Mark the replied-to finding as dismissed and confirm it."""
    comment = event["comment"]
    target_id = int(comment["in_reply_to_id"])
    reason = extract_reason(comment.get("body") or "")
    author = (comment.get("user") or {}).get("login", "someone")

    ledger, sticky = await github.load_ledger(number)
    if sticky is None:
        await github.reply_to_comment(
            number,
            target_id,
            "I have no review state on this pull request, so there is nothing "
            "for me to dismiss. Run a review first.",
        )
        return 0

    entry = _entry_for_comment(ledger.entries.values(), target_id)
    if entry is None:
        await github.reply_to_comment(
            number,
            target_id,
            "I could not match that to a finding I reported, so nothing was "
            "dismissed. Reply directly to the review comment you want retired.",
        )
        return 0

    entry.status = "wontfix"
    entry.wontfix_reason = reason

    await github.upsert_sticky_comment(
        number, replace_marker(sticky.body, ledger), sticky
    )
    await github.reply_to_comment(
        number,
        target_id,
        f"Dismissed — I will not report this again on this pull request.\n\n"
        f"> {reason}\n\n"
        f"<sub>Recorded as `wontfix` by @{author}. Reasons are kept so review "
        f"criteria can be corrected later, rather than the same mistake being "
        f"silently repeated.</sub>",
    )
    return 0


def _entry_for_comment(entries, comment_id: int) -> LedgerEntry | None:
    return next(
        (entry for entry in entries if entry.review_comment_id == comment_id), None
    )
