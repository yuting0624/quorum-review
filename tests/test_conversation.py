from src import conversation, prompts
from src.ledger import Ledger, LedgerEntry
from src.schema import PRContext

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
