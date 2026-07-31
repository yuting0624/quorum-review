"""Answering questions asked in the thread under a finding.

A finding without a way to argue with it is a verdict handed down: the only
moves left are to fix it or ignore it, and people who cannot argue eventually
stop reading. Replying ``@quorum`` in the thread asks the reviewer to defend or
withdraw what it said, where it said it.

The model that answers is **the one that reported the finding**. It is being
asked about its own claim, so the alternative — a model that never made the
claim inventing a defence of it — would be worse than useless.
"""

from __future__ import annotations

import re
from typing import Any

from . import prompts  # noqa: F401  (kept for symmetry with the prompt module)
from .github_client import GitHubClient
from .ledger import Ledger, LedgerEntry
from .providers.base import ReviewProvider
from .schema import Discussion, PRContext

MENTION = "@quorum"

#: Handled elsewhere: a re-review request and a dismissal are not questions.
_NOT_A_QUESTION = re.compile(r"@quorum\s+(/review|wontfix|false positive|誤検知)", re.I)


def is_question(event: dict[str, Any]) -> bool:
    """Whether this is someone asking the reviewer something in a thread."""
    comment = event.get("comment")
    if not isinstance(comment, dict) or not comment.get("in_reply_to_id"):
        return False

    body = comment.get("body") or ""
    if MENTION.lower() not in body.lower():
        return False
    return not _NOT_A_QUESTION.search(body)


def build_discussion(
    entry: LedgerEntry | None, comments: list[dict[str, Any]], fallback_path: str
) -> Discussion:
    """Assemble the finding and the conversation so far."""
    transcript = [
        (
            (comment.get("user") or {}).get("login", "someone"),
            (comment.get("body") or "").strip(),
        )
        for comment in comments
        if (comment.get("body") or "").strip()
    ]

    # The finding's original wording is already the first message in the
    # thread, so `body` carries what the ledger knows and the comment does not:
    # how it was classified, and whether a second model had looked at it.
    context = ""
    if entry:
        context = f"Recorded as {entry.severity} {entry.category}."
        if entry.verifier_model:
            context += (
                f" {entry.verifier_model} was asked to judge it independently "
                f"and said: {entry.verifier_reason}"
            )

    return Discussion(
        file_path=entry.file_path if entry else fallback_path,
        line=entry.line if entry else 0,
        title=entry.title if entry else "",
        body=context,
        transcript=transcript,
    )


def answering_model(entry: LedgerEntry | None, models: list[str]) -> str:
    """Pick who answers: whoever made the claim being questioned.

    A model that never reported the finding would have to invent a defence of
    someone else's reasoning, which is exactly the behaviour the review stages
    go out of their way to avoid.
    """
    if entry and entry.reported_by:
        for model in entry.reported_by:
            if model in models:
                return model
    return models[0]


def find_entry(ledger: Ledger, comment_id: int) -> LedgerEntry | None:
    return next(
        (e for e in ledger.entries.values() if e.review_comment_id == comment_id), None
    )


async def handle(
    github: GitHubClient,
    provider: ReviewProvider,
    ctx: PRContext,
    ledger: Ledger,
    event: dict[str, Any],
) -> int:
    """Answer the question, in the thread it was asked in."""
    comment = event["comment"]
    root_id = int(comment["in_reply_to_id"])
    number = ctx.number

    entry = find_entry(ledger, root_id)
    thread = await github.thread_comments(number, root_id)
    discussion = build_discussion(entry, thread, comment.get("path") or "")

    model = answering_model(entry, list(provider.models))
    try:
        answer = await provider.discuss(model, discussion, ctx)
    except Exception as error:  # noqa: BLE001 - a failed reply must still reply
        await github.reply_to_comment(
            number,
            root_id,
            f"I could not answer that — `{model}` failed with: {error}",
        )
        return 1

    if not answer.strip():
        answer = "I do not have enough here to answer that."

    await github.reply_to_comment(
        number,
        root_id,
        f"{answer.strip()}\n\n<sub>Answered by `{model}`, which reported this "
        f"finding. Reply `@quorum wontfix — <reason>` to retire it.</sub>",
    )
    return 0
