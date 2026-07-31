"""Authorisation for reviewing someone else's code with your own credentials.

Under `pull_request_target` the run holds a write-scoped token and the project's
secrets while the pull request belongs to a stranger. The workflow's `if:` is
the first gate; these are the checks that hold when someone edits it wrong,
which is the realistic failure — a condition in YAML that nobody tests.
"""

from __future__ import annotations

import asyncio

import pytest

from quorum_review import forks


def event(
    *,
    fork: bool = True,
    labels: tuple[str, ...] = (),
    sender: str = "maintainer",
    head_id: int = 2,
    base_id: int = 1,
) -> dict:
    return {
        "sender": {"login": sender},
        "pull_request": {
            "number": 7,
            "labels": [{"name": name} for name in labels],
            "head": {"repo": {"fork": fork, "id": head_id}},
            "base": {"repo": {"fork": False, "id": base_id}},
        },
    }


class FakeGitHub:
    def __init__(self, writers: tuple[str, ...] = ("maintainer",)) -> None:
        self._writers = set(writers)
        self.asked: list[str] = []

    async def has_write_access(self, username: str) -> bool:
        self.asked.append(username)
        return username in self._writers


def refuse(payload: dict, github: FakeGitHub | None = None) -> str:
    return asyncio.run(forks.refusal(github or FakeGitHub(), payload))


@pytest.fixture(autouse=True)
def default_label(monkeypatch):
    monkeypatch.delenv("QUORUM_FORK_LABEL", raising=False)


# -- recognising a fork ----------------------------------------------------


def test_a_same_repository_pull_request_is_not_a_fork():
    assert not forks.is_fork_event(event(fork=False, head_id=1, base_id=1))


def test_a_fork_flag_is_enough():
    assert forks.is_fork_event(event(fork=True))


def test_differing_repository_ids_are_enough():
    """A pull request from another repository in the same org has fork=false."""
    assert forks.is_fork_event(event(fork=False, head_id=99, base_id=1))


def test_a_payload_without_a_pull_request_is_not_a_fork():
    assert not forks.is_fork_event({"sender": {"login": "someone"}})


# -- the two conditions ----------------------------------------------------


def test_a_same_repository_pull_request_needs_no_label():
    assert refuse(event(fork=False, head_id=1, base_id=1)) == ""


def test_an_unlabelled_fork_is_refused():
    message = refuse(event(labels=()))
    assert "does not carry" in message
    assert "quorum: review" in message


def test_a_labelled_fork_from_a_maintainer_is_allowed():
    assert refuse(event(labels=("quorum: review",))) == ""


def test_a_label_applied_by_someone_without_write_access_is_refused():
    """Triage collaborators can label. That must not be authorisation."""
    github = FakeGitHub(writers=("maintainer",))
    message = refuse(event(labels=("quorum: review",), sender="drive-by"), github)
    assert "does not have write access" in message
    assert github.asked == ["drive-by"]


def test_permission_is_checked_against_the_api_not_the_payload():
    """author_association cannot tell a read-only collaborator from a maintainer."""
    github = FakeGitHub(writers=())
    assert refuse(event(labels=("quorum: review",)), github) != ""
    assert github.asked == ["maintainer"]


def test_the_label_is_matched_case_insensitively():
    assert refuse(event(labels=("Quorum: Review",))) == ""


def test_the_label_is_read_from_the_pull_request_not_the_event():
    """A push to an already-labelled branch must not need re-labelling."""
    payload = event(labels=("quorum: review",))
    assert "label" not in payload  # no `event.label`, as on a synchronize event
    assert refuse(payload) == ""


def test_the_label_name_is_configurable(monkeypatch):
    monkeypatch.setenv("QUORUM_FORK_LABEL", "ok-to-review")
    assert refuse(event(labels=("ok-to-review",))) == ""
    assert refuse(event(labels=("quorum: review",))) != ""


def test_an_empty_label_setting_falls_back_to_the_default(monkeypatch):
    """An unset action input arrives as an empty string, not as absent."""
    monkeypatch.setenv("QUORUM_FORK_LABEL", "")
    assert forks.review_label() == forks.DEFAULT_LABEL


def test_an_unknown_actor_is_refused():
    assert "does not have write access" in refuse(
        event(labels=("quorum: review",), sender=""), FakeGitHub(writers=())
    )


def test_the_actor_falls_back_to_the_runner_when_the_payload_has_no_sender(
    monkeypatch,
):
    monkeypatch.setenv("GITHUB_ACTOR", "someone")
    payload = event(labels=("quorum: review",))
    del payload["sender"]
    assert forks.actor(payload) == "someone"
