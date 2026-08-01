"""`/criteria` is a fork-authorised path, and it must ask about the real actor.

The criteria are instructions to a model, and this path reads them as trusted.
That argument only holds if a fork cannot choose which criteria are read, so
the handler applies the same rule the review path does.

Two bugs lived here, both found by the reviewer on the pull request that added
the check:

1. It detected a fork to pick the ref and then proceeded anyway — half a check.
2. It then passed a synthetic ``{"pull_request": pull}`` payload to the
   authorisation, which has no ``sender``, so the identity check fell back to
   ``GITHUB_ACTOR``. For ``issue_comment`` that is the commenter, so it worked
   by coincidence; an authorisation check should not.
"""

from __future__ import annotations

import pytest

from quorum_review import forks, review
from quorum_review.ledger import Ledger
from quorum_review.providers.base import ProviderUnavailable


def pull(fork: bool = True, labels: tuple[str, ...] = ()) -> dict:
    return {
        "number": 1,
        "head": {"sha": "head1234", "repo": {"id": 2 if fork else 1, "fork": fork}},
        "base": {"sha": "base5678", "repo": {"id": 1, "fork": False}},
        "labels": [{"name": name} for name in labels],
    }


class FakeGitHub:
    """Only what the refusal path touches; a model call would fail loudly."""

    def __init__(self, pull_payload: dict, writers: set[str]):
        self._pull = pull_payload
        self._writers = writers
        self.posted: list[str] = []
        self.asked_about: list[str] = []

    async def load_ledger(self, number: int):
        ledger = Ledger.empty(number)
        ledger.entries = {
            entry.finding_id: entry
            for entry in (dismissed_entry(n) for n in range(learning_minimum()))
        }
        return ledger, None

    async def pull_request(self, number: int) -> dict:
        return self._pull

    async def has_write_access(self, login: str) -> bool:
        self.asked_about.append(login)
        return login in self._writers

    async def post_issue_comment(self, number: int, body: str) -> None:
        self.posted.append(body)


def learning_minimum() -> int:
    from quorum_review import learning

    return learning.MIN_DISMISSALS


def dismissed_entry(n: int):
    from quorum_review.ledger import LedgerEntry

    return LedgerEntry(
        finding_id=f"{n:016x}",
        file_path="app/x.py",
        category="security",
        severity="high",
        title=f"finding {n}",
        status="wontfix",
        wontfix_reason="not a concern here",
    )


def comment_event(login: str) -> dict:
    """An `issue_comment` payload, which carries no `pull_request` object."""
    return {
        "action": "created",
        "sender": {"login": login, "type": "User"},
        "comment": {"body": "@quorum /criteria", "user": {"login": login}},
        "issue": {"number": 1, "pull_request": {"url": "..."}},
    }


@pytest.mark.asyncio
async def test_an_unlabelled_fork_is_refused_before_any_model_call():
    github = FakeGitHub(pull(), writers={"yuting0624"})

    assert (
        await review.propose_criteria(
            github, 1, "security-review", comment_event("drive-by")
        )
        == 0
    )
    assert "does not carry" in github.posted[0]


@pytest.mark.asyncio
async def test_the_label_alone_is_not_enough(monkeypatch):
    """Labelling is available to triage collaborators."""
    monkeypatch.setenv("QUORUM_FORK_LABEL", "quorum:review-fork")
    github = FakeGitHub(pull(labels=("quorum:review-fork",)), writers={"yuting0624"})

    assert (
        await review.propose_criteria(
            github, 1, "security-review", comment_event("triager")
        )
        == 0
    )
    assert "does not have write access" in github.posted[0]


@pytest.mark.asyncio
async def test_the_commenter_is_who_gets_checked(monkeypatch):
    """Not GITHUB_ACTOR. The synthetic payload the first fix passed had no
    `sender`, so the check fell through to the environment — which is the
    commenter for `issue_comment` and something else for everything that is
    not."""
    monkeypatch.setenv("QUORUM_FORK_LABEL", "quorum:review-fork")
    monkeypatch.setenv("GITHUB_ACTOR", "yuting0624")
    github = FakeGitHub(pull(labels=("quorum:review-fork",)), writers={"yuting0624"})

    await review.propose_criteria(github, 1, "security-review", comment_event("triager"))

    assert github.asked_about == ["triager"]


@pytest.mark.asyncio
async def test_a_pull_request_from_the_repository_itself_is_not_refused(monkeypatch):
    """`refusal` returns "" and the handler goes on to build a provider, which
    is unreachable here — getting that far is the assertion. Nothing is
    posted, so the branch was not taken."""
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    github = FakeGitHub(pull(fork=False), writers=set())

    with pytest.raises(ProviderUnavailable):
        await review.propose_criteria(
            github, 1, "security-review", comment_event("yuting0624")
        )
    assert github.posted == []


def test_the_synthetic_payload_loses_the_sender():
    """Directly, so the reason the test above exists does not depend on
    reading the handler."""
    event = comment_event("triager")

    assert forks.actor(event) == "triager"
    assert forks.actor({"pull_request": pull()}) != "triager"
