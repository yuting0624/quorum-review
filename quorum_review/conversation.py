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

from . import prompts
from . import workspace as workspace_mod
from .github_client import GitHubClient
from .ledger import FINDING_FOOTER, Ledger, LedgerEntry
from .matching import mention
from .providers.base import ReviewProvider
from .schema import Discussion, PRContext

#: How much of a thread reaches the model, newest first. A thread is a place
#: anyone can add text, and all of it was going into the prompt: fifty replies
#: of padding cost real money and push the finding itself out of view.
MAX_TRANSCRIPT_COMMENTS = 20
MAX_TRANSCRIPT_CHARS = 4_000

#: Tool calls one answer may make. A question is narrow — "does X cover this
#: case?" — and the person asking is waiting for the reply in the thread.
QUESTION_TOOL_CALLS = 8


#: Handled elsewhere: a re-review request and a dismissal are not questions.
def _not_a_question() -> re.Pattern[str]:
    """A re-review request and a dismissal are handled elsewhere."""
    return re.compile(
        re.escape(mention()) + r"\s+(/review|wontfix|false positive|誤検知)", re.I
    )


def is_question(event: dict[str, Any]) -> bool:
    """Whether this is someone asking the reviewer something in a thread."""
    comment = event.get("comment")
    if not isinstance(comment, dict) or not comment.get("in_reply_to_id"):
        return False

    body = comment.get("body") or ""
    if mention().lower() not in body.lower():
        return False
    return not _not_a_question().search(body)


def owns_thread(comments: list[dict[str, Any]], root_id: int) -> bool:
    """Whether the reviewer is the one who started this thread.

    ``@quorum`` in a reply used to answer anything, including a conversation
    between two people that the reviewer had no part in. That is a model call
    anyone able to comment can trigger, and the answer opens by referring to a
    finding that never existed.

    The footer alone does not settle it, which was the first attempt: anyone
    can type ``<sub>`security` · id `deadbeef`</sub>`` into a comment and then
    reply to themselves. A footer is a label, not a signature.

    So the author has to be a bot as well. That part GitHub enforces — a person
    commenting is ``type: User`` and cannot say otherwise — and both the
    Actions app and a GitHub App token report ``Bot``. Running under a personal
    token means this returns False and a thread with no ledger entry is
    declined, which is the safe direction for a check that exists to stop work
    being triggered on threads that are not ours.
    """
    root = next(
        (c for c in comments if int(c.get("id") or 0) == root_id),
        None,
    )
    if root is None:
        return False
    if ((root.get("user") or {}).get("type") or "") != "Bot":
        return False
    return bool(FINDING_FOOTER.search(root.get("body") or ""))


def build_discussion(
    entry: LedgerEntry | None, comments: list[dict[str, Any]], fallback_path: str
) -> Discussion:
    """Assemble the finding and the conversation so far.

    Bounded, and the first comment is always kept. Taking the newest N was the
    first attempt and it drops the root — which is the finding itself, and on a
    thread whose ledger entry is gone it is the only place the finding exists.
    Everything here is text somebody else wrote in a place anyone can write,
    and all of it was reaching the prompt.
    """
    said = [
        (
            (comment.get("user") or {}).get("login", "someone"),
            (comment.get("body") or "").strip()[:MAX_TRANSCRIPT_CHARS],
        )
        for comment in comments
        if (comment.get("body") or "").strip()
    ]
    if len(said) > MAX_TRANSCRIPT_COMMENTS:
        said = [said[0], *said[-(MAX_TRANSCRIPT_COMMENTS - 1) :]]
    transcript = said

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

    # No finding here and no sign the reviewer started the thread. Answering
    # would be a model call anybody can trigger on any conversation, and the
    # reply would open by referring to a finding that never existed.
    if entry is None and not owns_thread(thread, root_id):
        await github.reply_to_comment(
            number,
            root_id,
            "This is not one of my threads, so I have no finding to discuss. "
            f"Ask under a review comment I posted, or `{mention()} /review` to "
            "re-review the pull request.",
        )
        return 0

    discussion = build_discussion(entry, thread, comment.get("path") or "")

    model = answering_model(entry, list(provider.models))
    # A question in a thread is the case where reading past the diff matters
    # most: someone is pushing back, usually with "but X handles that", and the
    # honest answer requires opening X rather than restating the finding.
    budgets = workspace_mod.build(1, QUESTION_TOOL_CALLS, ctx.exclude_patterns)
    toolbox = next(iter(budgets), None)
    try:
        answer = await provider.respond(
            model,
            prompts.discuss_system(
                getattr(provider, "language", ""), tools=toolbox is not None
            ),
            prompts.discuss_user(discussion, ctx),
            toolbox=toolbox,
        )
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
        f"finding. Reply `{mention()} wontfix — <reason>` to retire it.</sub>",
    )
    return 0
