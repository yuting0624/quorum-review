from quorum_review import conversation, prompts
from quorum_review.ledger import Ledger, LedgerEntry
from quorum_review.schema import PRContext

CTX = PRContext(
    owner="o",
    repo="r",
    number=1,
    head_sha="abc1234",
    base_sha="def5678",
    title="t",
    body="b",
    diff="diff --git a/app/auth.py b/app/auth.py\n+if token == expected:\n",
)


def reply(body, in_reply_to=1234):
    comment = {"body": body, "user": {"login": "someone"}}
    if in_reply_to is not None:
        comment["in_reply_to_id"] = in_reply_to
    return {"comment": comment}


def entry(**overrides):
    defaults = dict(
        finding_id="f1",
        file_path="app/auth.py",
        category="security",
        severity="high",
        title="Token compared with ==",
        line=20,
        snippet="if token == expected:",
        reported_by=["claude-opus-5"],
        review_comment_id=1234,
    )
    return LedgerEntry(**{**defaults, **overrides})


# -- recognising a question ------------------------------------------------


def test_a_mention_in_a_thread_is_a_question():
    assert conversation.is_question(reply("@quorum why is this exploitable?"))


def test_a_top_level_mention_is_not():
    """Without a reply target there is no finding to discuss."""
    assert not conversation.is_question(reply("@quorum why?", in_reply_to=None))


def test_commands_are_not_questions():
    """Dismissal and re-review are handled by other paths."""
    assert not conversation.is_question(reply("@quorum wontfix — guarded upstream"))
    assert not conversation.is_question(reply("@quorum false positive"))
    assert not conversation.is_question(reply("@quorum 誤検知です"))
    assert not conversation.is_question(reply("@quorum /review"))


def test_a_reply_without_a_mention_is_not_a_question():
    assert not conversation.is_question(reply("thanks, fixing"))


# -- who answers -----------------------------------------------------------


def test_the_model_that_made_the_claim_answers():
    """Anyone else would have to invent a defence of someone else's reasoning."""
    models = ["gemini-3.6-flash", "claude-opus-5"]
    assert conversation.answering_model(entry(), models) == "claude-opus-5"


def test_it_falls_back_when_the_reporter_is_no_longer_configured():
    assert (
        conversation.answering_model(entry(reported_by=["retired-model"]), ["gemini-x"])
        == "gemini-x"
    )


def test_it_falls_back_when_the_thread_matches_no_finding():
    assert conversation.answering_model(None, ["gemini-x", "claude-y"]) == "gemini-x"


# -- assembling the context ------------------------------------------------


def test_the_transcript_is_included_in_order():
    comments = [
        {"user": {"login": "quorum"}, "body": "Token compared with =="},
        {"user": {"login": "yuting"}, "body": "@quorum is that reachable?"},
    ]
    discussion = conversation.build_discussion(entry(), comments, "")
    assert [author for author, _ in discussion.transcript] == ["quorum", "yuting"]


def test_empty_comments_are_dropped():
    comments = [
        {"user": {"login": "a"}, "body": "  "},
        {"user": {"login": "b"}, "body": "x"},
    ]
    assert len(conversation.build_discussion(entry(), comments, "").transcript) == 1


def test_the_ledger_context_says_what_the_comment_does_not():
    """The original wording is already the first message; don't repeat it."""
    discussion = conversation.build_discussion(
        entry(verifier_model="gemini-3.6-flash", verifier_reason="traced the path"),
        [],
        "",
    )
    assert "high security" in discussion.body
    assert "gemini-3.6-flash" in discussion.body
    assert "traced the path" in discussion.body


def test_a_thread_with_no_matching_finding_still_works():
    """The finding may have been dismissed or resolved since."""
    discussion = conversation.build_discussion(None, [], "app/x.py")
    assert discussion.file_path == "app/x.py"
    assert discussion.title == ""


def test_find_entry_matches_on_the_root_comment():
    ledger = Ledger.empty(1)
    ledger.record(entry())
    assert conversation.find_entry(ledger, 1234) is not None
    assert conversation.find_entry(ledger, 9999) is None


# -- the prompt ------------------------------------------------------------


def test_the_prompt_labels_the_conversation_as_untrusted():
    discussion = conversation.build_discussion(
        entry(), [{"user": {"login": "x"}, "body": "@quorum why?"}], ""
    )
    rendered = prompts.discuss_user(discussion, CTX)
    assert "<untrusted_conversation>" in rendered
    assert "<untrusted_diff" in rendered
    assert "why?" in rendered


def test_the_prompt_offers_the_dismissal_route():
    """A reviewer that cannot be wrong is one people route around."""
    system = " ".join(prompts.discuss_system().split())
    assert "wontfix" in system
    assert "no json schema" in system.lower()


# -- whose thread is this ---------------------------------------------------


def _root(comment_id: int, with_footer: bool = True, author_type: str = "Bot") -> dict:
    """A root comment as the reviewer posts it and GitHub returns it."""
    footer = "\n\n<sub>`security` · id `abc123def4560000`</sub>" if with_footer else ""
    return {
        "id": comment_id,
        "user": {"login": "github-actions[bot]", "type": author_type},
        "body": f"🔴 **Something is wrong**\n\nbody{footer}",
    }


def _reply(comment_id: int, root: int, body: str, author="someone") -> dict:
    return {
        "id": comment_id,
        "in_reply_to_id": root,
        "user": {"login": author},
        "body": body,
    }


def test_a_thread_the_reviewer_started_is_recognised():
    from quorum_review.conversation import owns_thread

    assert owns_thread([_root(11), _reply(12, 11, "@quorum why?")], 11)


def test_a_conversation_between_two_people_is_not():
    """`@quorum` used to answer anything, including threads the reviewer had no
    part in — a model call anyone able to comment can trigger, answered with a
    reference to a finding that never existed."""
    from quorum_review.conversation import owns_thread

    assert not owns_thread(
        [_root(11, with_footer=False), _reply(12, 11, "@quorum thoughts?")], 11
    )


def test_a_thread_whose_root_is_missing_is_not_ours():
    from quorum_review.conversation import owns_thread

    assert not owns_thread([_reply(12, 11, "@quorum why?")], 11)


# -- how much of a thread reaches the model ---------------------------------


def test_the_transcript_is_capped_at_a_number_of_comments():
    """A thread is a place anyone can add text, and all of it was going into
    the prompt."""
    from quorum_review.conversation import MAX_TRANSCRIPT_COMMENTS, build_discussion

    many = [_reply(n, 11, f"comment {n}") for n in range(MAX_TRANSCRIPT_COMMENTS + 15)]
    discussion = build_discussion(None, many, "a.py")

    assert len(discussion.transcript) == MAX_TRANSCRIPT_COMMENTS


def test_the_newest_comments_are_the_ones_kept():
    """The question being answered is the last one."""
    from quorum_review.conversation import MAX_TRANSCRIPT_COMMENTS, build_discussion

    many = [_reply(n, 11, f"comment {n}") for n in range(MAX_TRANSCRIPT_COMMENTS + 5)]
    discussion = build_discussion(None, many, "a.py")

    assert discussion.transcript[-1][1] == f"comment {MAX_TRANSCRIPT_COMMENTS + 4}"


def test_one_very_long_comment_is_capped_too():
    """Otherwise the count limit is no limit at all."""
    from quorum_review.conversation import MAX_TRANSCRIPT_CHARS, build_discussion

    discussion = build_discussion(None, [_reply(12, 11, "x" * 50_000)], "a.py")
    assert len(discussion.transcript[0][1]) == MAX_TRANSCRIPT_CHARS


def test_a_short_thread_is_untouched():
    from quorum_review.conversation import build_discussion

    discussion = build_discussion(
        None, [_root(11), _reply(12, 11, "@quorum why?")], "a.py"
    )
    assert len(discussion.transcript) == 2


def test_a_footer_a_person_typed_does_not_make_a_thread_ours():
    """The first version of this check trusted the footer alone. Anyone can
    type `<sub>`security` · id `deadbeef`</sub>` into a comment and reply to
    themselves; a footer is a label, not a signature."""
    from quorum_review.conversation import owns_thread

    forged = _root(11, author_type="User")
    assert not owns_thread([forged, _reply(12, 11, "@quorum why?")], 11)


def test_the_root_comment_is_always_kept():
    """Taking the newest N drops the finding itself — and on a thread whose
    ledger entry is gone, the root comment is the only place it exists."""
    from quorum_review.conversation import MAX_TRANSCRIPT_COMMENTS, build_discussion

    thread = [_root(11)] + [
        _reply(n, 11, f"comment {n}") for n in range(MAX_TRANSCRIPT_COMMENTS + 10)
    ]
    discussion = build_discussion(None, thread, "a.py")

    assert len(discussion.transcript) == MAX_TRANSCRIPT_COMMENTS
    assert "Something is wrong" in discussion.transcript[0][1]
    assert discussion.transcript[-1][1].endswith(str(MAX_TRANSCRIPT_COMMENTS + 9))
