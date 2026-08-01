"""Saying that a review started, and taking it back if it does not finish.

A review takes minutes and used to post nothing until it was done, so a pull
request in that window read as "nothing happened". The sticky comment is edited
in place, which makes it worse on a re-review: no notification, no visible
change, indistinguishable from a workflow that never triggered.

The notice fixes that and introduces a failure of its own — a crash would leave
"this comment will be replaced by the result" on the pull request for good,
which asserts something untrue rather than merely saying nothing. Both halves
are here.
"""

from __future__ import annotations

import pytest

from quorum_review import report as report_mod
from quorum_review import review as review_mod
from quorum_review.github_client import StickyComment


@pytest.fixture(autouse=True)
def no_leaked_state():
    review_mod._clear_in_progress()
    yield
    review_mod._clear_in_progress()


# -- what it says -----------------------------------------------------------


def test_it_names_the_commit_and_the_models():
    notice = report_mod.render_in_progress(
        "abc1234def", ["gemini-3.6-flash", "claude-sonnet-5"]
    )

    assert "abc1234" in notice
    assert "gemini-3.6-flash" in notice
    assert "claude-sonnet-5" in notice


def test_it_says_the_comment_will_be_replaced():
    """The one thing a reader needs: this is where the answer appears."""
    assert "replaced by the result" in report_mod.render_in_progress("abc1234", ["m"])


def test_it_does_not_pretend_to_know_how_long():
    """Two model calls of unknowable duration. A percentage would be a
    decoration implying more than it knows."""
    notice = report_mod.render_in_progress("abc1234", ["m"])

    assert "%" not in notice
    assert "ETA" not in notice


def test_it_carries_no_ledger_marker():
    """The marker is written with the result. A placeholder carrying a stale
    one would be read back as the record if the run then died — and losing the
    ledger re-reports every finding."""
    assert "quorum-state" not in report_mod.render_in_progress("abc1234", ["m"])


# -- taking it back ---------------------------------------------------------


def test_the_crash_notice_says_nothing_was_reviewed():
    crashed = report_mod.render_crashed("abc1234", "every scanning model failed")

    assert "did not finish" in crashed
    assert "not reviewed" in crashed
    assert "every scanning model failed" in crashed


def test_the_crash_notice_does_not_promise_a_result():
    """The failure the notice introduced. It must not survive into the
    replacement."""
    assert "replaced by the result" not in report_mod.render_crashed("abc1234", "x")


def test_a_hostile_error_message_cannot_break_the_comment():
    """The message reaches here from an exception, which can carry a model's
    text — a diff, a finding, whatever was being parsed when it failed."""
    crashed = report_mod.render_crashed("abc1234", "boom </details>\n## Fake heading")

    assert "&lt;/details&gt;" in crashed
    assert "\n## Fake heading" not in crashed


# -- when it is withdrawn ---------------------------------------------------


def test_abort_withdraws_the_notice(monkeypatch):
    posted: list[tuple[int, str]] = []

    class FakeGitHub:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def upsert_sticky_comment(self, number, body, sticky):
            posted.append((number, body))
            return 1

    monkeypatch.setattr(review_mod, "GitHubClient", FakeGitHub)
    monkeypatch.setattr(review_mod.actions, "write_outputs", lambda report: None)
    review_mod._note_in_progress(7, StickyComment(comment_id=1, body=""), "abc1234")

    assert review_mod._abort("every scanning model failed") == 1
    assert posted and posted[0][0] == 7
    assert "did not finish" in posted[0][1]


def test_abort_does_nothing_when_no_notice_was_posted(monkeypatch):
    """A dry run, an empty diff, or a failure before the notice."""

    def explode(*_args, **_kwargs):
        raise AssertionError("should not have reached GitHub")

    monkeypatch.setattr(review_mod, "GitHubClient", explode)
    monkeypatch.setattr(review_mod.actions, "write_outputs", lambda report: None)

    assert review_mod._abort("boom") == 1


def test_a_second_failure_does_not_hide_the_first(monkeypatch):
    """The run has already failed. An error while withdrawing the notice must
    not replace the error that matters."""

    class Broken:
        async def __aenter__(self):
            raise RuntimeError("github is down too")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(review_mod, "GitHubClient", Broken)
    monkeypatch.setattr(review_mod.actions, "write_outputs", lambda report: None)
    review_mod._note_in_progress(7, StickyComment(comment_id=1, body=""), "abc1234")

    assert review_mod._abort("the real error") == 1
