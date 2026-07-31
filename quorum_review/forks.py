"""Reviewing a pull request that comes from a fork.

Forks are where a review bot either earns its place or becomes the reason
external contributions go unreviewed. They are also where review bots get
compromised, so the reasoning matters more than the code.

**Why it needs a different trigger.** A `pull_request` run from a fork gets a
read-only `GITHUB_TOKEN` and no secrets. It cannot post a comment and cannot
reach Vertex, so it fails noisily on every outside contribution.
`pull_request_target` fixes that by running in the base repository's context —
with write access and secrets — while the code under review is someone else's.

**Why that is normally a mistake, and why it is not one here.** The published
attacks on `pull_request_target` all have the same shape: the workflow checks
out the fork's code and then *executes* it — `npm install`, a build step, a
test run, a linter that loads a config file. This action never does. It
installs itself from the action's own path, and the fork's code is read as
text. There is no step in which a fork controls anything that runs.

**Two conditions, both required.** The event is gated on a label, and the label
must have been applied by someone with write access — checked here against the
API, not inferred from `author_association`, which cannot tell a read-only
collaborator from a maintainer. The workflow's `if:` enforces the first cheaply;
this module enforces both, because a workflow condition is one careless edit
away from being wrong and the failure is silent.

**What the fork still does not get to decide.** Its `.quorumignore` is ignored
in favour of the base branch's — see `github_client.load_context`. A file that
can only remove things from review is a file an untrusted head should not be
able to add.
"""

from __future__ import annotations

import os
from typing import Any

#: Applying this label is the act that authorises a fork review. Configurable
#: because organisations already have label conventions, and a bot that demands
#: its own vocabulary gets less use.
DEFAULT_LABEL = "quorum: review"


def review_label() -> str:
    return os.getenv("QUORUM_FORK_LABEL", DEFAULT_LABEL).strip() or DEFAULT_LABEL


def is_fork_event(event: dict[str, Any]) -> bool:
    """Whether this payload describes a pull request from another repository."""
    pull = event.get("pull_request")
    if not isinstance(pull, dict):
        return False
    head_repo = (pull.get("head") or {}).get("repo") or {}
    base_repo = (pull.get("base") or {}).get("repo") or {}
    if head_repo.get("fork"):
        return True
    head_id, base_id = head_repo.get("id"), base_repo.get("id")
    return bool(head_id and base_id and head_id != base_id)


def carries_label(event: dict[str, Any]) -> bool:
    """Whether the review label is currently on the pull request.

    The pull request's label list is checked rather than `event.label`, so that
    a re-run — a push to an already-labelled branch, a manual dispatch — is
    still authorised without asking a maintainer to re-apply it.
    """
    pull = event.get("pull_request")
    if not isinstance(pull, dict):
        return False
    wanted = review_label().casefold()
    return any(
        (label or {}).get("name", "").casefold() == wanted
        for label in pull.get("labels") or []
    )


def actor(event: dict[str, Any]) -> str:
    """Who caused this run: whoever applied the label, or triggered the re-run."""
    sender = event.get("sender")
    if isinstance(sender, dict) and sender.get("login"):
        return str(sender["login"])
    return os.getenv("GITHUB_ACTOR", "").strip()


async def refusal(github: Any, event: dict[str, Any]) -> str:
    """Why this fork review must not proceed, or "" if it may.

    Returns prose rather than raising: a refusal is a normal outcome that the
    log should explain, not an error someone has to debug.
    """
    if not is_fork_event(event):
        return ""

    label = review_label()
    if not carries_label(event):
        return (
            f"this pull request is from a fork and does not carry the "
            f"{label!r} label, so it was not reviewed. A maintainer can add "
            f"the label to authorise a review of this branch's code."
        )

    who = actor(event)
    if not await github.has_write_access(who):
        return (
            f"the {label!r} label was applied by {who or 'an unknown user'}, "
            f"who does not have write access to this repository. Labelling is "
            f"available to triage collaborators, so the label alone cannot be "
            f"the authorisation."
        )
    return ""
