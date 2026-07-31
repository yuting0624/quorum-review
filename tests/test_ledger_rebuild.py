"""What survives when someone deletes the summary comment.

The ledger lives in a hidden marker inside that comment. Tidying a noisy pull
request is a normal thing to do and nothing warns anyone, so the state can
simply vanish — and then the next review has no history: every open finding is
posted a second time, and every `wontfix` somebody took the trouble to justify
is silently undone.

The comments the reviewer already posted carry enough to stop that.
"""

from __future__ import annotations

from quorum_review.ledger import rebuild
from quorum_review.schema import Finding


def posted(comment_id: int, finding_id: str, path="app/search.py", line=18,
           title="SQL injection via f-string", category="security") -> dict:
    """An inline comment as `render_inline` writes it and GitHub returns it."""
    return {
        "id": comment_id,
        "path": path,
        "line": line,
        "body": (
            f"\U0001f534 **{title}**\n\n"
            "The search term reaches the query unparameterised.\n\n"
            f"<sub>`{category}` \u00b7 id `{finding_id}`</sub>"
        ),
    }


def reply(comment_id: int, root: int, body: str) -> dict:
    return {"id": comment_id, "in_reply_to_id": root, "body": body}


def finding(finding_id: str, path="app/search.py", line=18) -> Finding:
    return Finding(
        file_path=path,
        line=line,
        category="security",
        severity="high",
        title="SQL injection via f-string",
        body="b",
        code_snippet="s",
        finding_id=finding_id,
        reported_by=["gemini-3.6-flash"],
    )


# -- recovering identity ---------------------------------------------------


def test_a_posted_finding_is_recovered_from_its_footer():
    book = rebuild(7, [posted(11, "abc123def4560000")], dismissed=set())
    entry = book.entries["abc123def4560000"]

    assert entry.file_path == "app/search.py"
    assert entry.line == 18
    assert entry.category == "security"
    assert entry.title == "SQL injection via f-string"
    assert entry.review_comment_id == 11
    assert entry.status == "open"


def test_a_recovered_finding_is_not_reported_again():
    """The whole point. Without this the next review posts a second copy of
    every comment already on the pull request."""
    book = rebuild(7, [posted(11, "abc123def4560000")], dismissed=set())
    assert book.is_suppressed(finding("abc123def4560000"))


def test_it_matches_positionally_too():
    """Models do not quote the same defect the same way twice, so the ID from
    the next run will differ. Location is what carries across."""
    book = rebuild(7, [posted(11, "abc123def4560000")], dismissed=set())
    assert book.is_suppressed(finding("a-completely-different-id"))


def test_comments_from_people_are_ignored():
    human = {"id": 20, "path": "app/x.py", "line": 4, "body": "looks fine to me"}
    assert rebuild(7, [human], dismissed=set()).entries == {}


def test_replies_are_not_mistaken_for_findings():
    comments = [posted(11, "abc123def4560000"), reply(12, 11, "thanks, fixed")]
    assert len(rebuild(7, comments, dismissed=set()).entries) == 1


def test_a_comment_without_a_footer_is_not_a_finding():
    """An older version, or something a human wrote that looks similar."""
    stray = {"id": 30, "path": "a.py", "line": 1, "body": "**Bold**, but no id"}
    assert rebuild(7, [stray], dismissed=set()).entries == {}


# -- recovering dismissals -------------------------------------------------


def test_a_dismissal_survives_the_summary_being_deleted():
    """This is the half that matters most. A re-raised finding is noise; a
    re-raised finding somebody explicitly retired is the reviewer overruling a
    person."""
    comments = [
        posted(11, "abc123def4560000"),
        reply(12, 11, "@quorum wontfix — guarded by the ORM"),
    ]
    book = rebuild(7, comments, dismissed={11})

    entry = book.entries["abc123def4560000"]
    assert entry.status == "wontfix"
    assert entry.wontfix_reason
    assert book.is_suppressed(finding("abc123def4560000"))


def test_an_undismissed_finding_stays_open():
    comments = [posted(11, "a" * 16), reply(12, 11, "why is this a problem?")]
    assert rebuild(7, comments, dismissed=set()).entries["a" * 16].status == "open"


# -- what cannot be recovered ----------------------------------------------


def test_severity_is_not_invented():
    """It is not in the comment. Guessing high would let a rebuilt ledger fail
    a build that the original would have passed."""
    entry = rebuild(7, [posted(11, "b" * 16)], dismissed=set()).entries["b" * 16]
    assert entry.severity == "low"


def test_a_title_that_cannot_be_read_is_marked_as_such():
    odd = {
        "id": 40,
        "path": "a.py",
        "line": 1,
        "body": "no bold\n\n<sub>`security` \u00b7 id `cccccccccccccccc`</sub>",
    }
    entry = rebuild(7, [odd], dismissed=set()).entries["cccccccccccccccc"]
    assert entry.title == "(recovered finding)"


def test_nothing_posted_yet_rebuilds_to_nothing():
    assert rebuild(7, [], dismissed=set()).entries == {}


def test_the_summary_says_when_it_had_to_recover():
    """A run that lost its history and rebuilt part of it is not the same run
    as one that never lost anything, and the reader should not have to guess
    which they are looking at."""
    from quorum_review.report import RunReport, render

    body = render(RunReport(recovered=4))
    assert "had been deleted" in body
    assert "4 finding(s) were recovered" in body
    assert "could not be recovered" in body


def test_an_ordinary_run_says_nothing_about_recovery():
    from quorum_review.report import RunReport, render

    assert "recovered" not in render(RunReport(suppressed=2, suppressed_titles=["a"]))


# -- the four findings the reviewer raised on this change -------------------


def test_a_closed_finding_is_not_resurrected_as_open():
    """Worse than a duplicate. A fixed finding rebuilt as open is then
    *suppressed*, so a genuine regression at that location goes unreported."""
    comments = [
        posted(11, "d" * 16),
        reply(12, 11, "No longer reported as of `abc1234`. It will reappear if..."),
    ]
    book = rebuild(7, comments, dismissed=set(), closed={11})

    assert book.entries["d" * 16].status == "fixed"
    assert not book.is_suppressed(finding("d" * 16))


def test_a_dismissal_outranks_a_closure():
    """One is a decision someone made; the other is an observation."""
    book = rebuild(7, [posted(11, "e" * 16)], dismissed={11}, closed={11})
    assert book.entries["e" * 16].status == "wontfix"


class FakeGitHub:
    """Enough of the client to exercise the authorisation check."""

    def __init__(self, comments, writers):
        self._comments = comments
        self._writers = set(writers)
        self.asked: list[str] = []

    async def review_comments(self, number):
        return self._comments

    async def has_write_access(self, username):
        self.asked.append(username)
        return username in self._writers


def by(comment_id: int, root: int, author: str, body: str) -> dict:
    return {
        "id": comment_id,
        "in_reply_to_id": root,
        "user": {"login": author},
        "body": body,
    }


def rebuild_via_client(comments, writers):
    import asyncio

    from quorum_review.github_client import GitHubClient

    client = GitHubClient(token="t", repository="o/r")
    fake = FakeGitHub(comments, writers)
    client.review_comments = fake.review_comments  # type: ignore[method-assign]
    client.has_write_access = fake.has_write_access  # type: ignore[method-assign]
    return asyncio.run(client._rebuild_ledger(7, comments)), fake


def test_a_dismissal_from_someone_without_write_access_is_not_honoured():
    """The hole this recovery opened, reported by the reviewer.

    `dismissal.handle` checks write access before recording one, precisely
    because `COLLABORATOR` covers read-only and triage collaborators. Reading
    dismissals back out of a thread has to apply the same check, or recovery
    becomes a way around it.
    """
    comments = [
        posted(11, "f" * 16),
        by(12, 11, "drive-by", "@quorum wontfix — trust me"),
    ]
    book, fake = rebuild_via_client(comments, writers={"maintainer"})

    assert book.entries["f" * 16].status == "open"
    assert fake.asked == ["drive-by"]


def test_a_dismissal_from_a_maintainer_is_honoured():
    comments = [
        posted(11, "1a" * 8),
        by(12, 11, "maintainer", "@quorum wontfix — guarded by the ORM"),
    ]
    book, _ = rebuild_via_client(comments, writers={"maintainer"})
    assert book.entries["1a" * 8].status == "wontfix"


def test_each_author_is_only_asked_about_once():
    comments = [
        posted(11, "2b" * 8),
        posted(21, "3c" * 8, line=40),
        by(12, 11, "maintainer", "@quorum wontfix — one"),
        by(22, 21, "maintainer", "@quorum wontfix — two"),
    ]
    _book, fake = rebuild_via_client(comments, writers={"maintainer"})
    assert fake.asked == ["maintainer"]


def test_a_rebuilt_ledger_says_so_rather_than_being_guessed_at():
    """A marker that is present but will not decode looks exactly like one that
    decoded fine, so the caller cannot infer this."""
    book, _ = rebuild_via_client([posted(11, "4d" * 8)], writers=set())
    assert book.was_rebuilt is False  # _rebuild_ledger does not set it
    book.was_rebuilt = True
    from quorum_review.ledger import decode_marker, encode_marker

    assert decode_marker(encode_marker(book)).was_rebuilt is False


def test_a_footer_that_is_not_a_hex_digest_is_not_a_finding():
    """Caught by these tests using letters past 'f' as placeholder IDs. Real
    ones are sha256 prefixes, and anything else is someone imitating the
    format."""
    fake = {
        "id": 50,
        "path": "a.py",
        "line": 1,
        "body": "**x**\n\n<sub>`security` · id `zzzzzzzzzzzzzzzz`</sub>",
    }
    assert rebuild(7, [fake], dismissed=set()).entries == {}


def test_a_closure_reply_from_anyone_else_is_not_trusted():
    """The mirror of the dismissal hole, and not merely noise: a finding marked
    fixed is excluded from the SARIF upload, so a fake closure would close a
    code-scanning alert."""
    comments = [
        {**posted(11, "5e" * 8), "user": {"login": "github-actions[bot]"}},
        by(12, 11, "drive-by", "No longer reported as of `abc1234`."),
    ]
    book, _ = rebuild_via_client(comments, writers=set())
    assert book.entries["5e" * 8].status == "open"


def test_a_closure_reply_from_the_reviewer_is_trusted():
    comments = [
        {**posted(11, "6f" * 8), "user": {"login": "github-actions[bot]"}},
        by(12, 11, "github-actions[bot]", "No longer reported as of `abc1234`."),
    ]
    book, _ = rebuild_via_client(comments, writers=set())
    assert book.entries["6f" * 8].status == "fixed"
