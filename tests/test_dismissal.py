from quorum_review import dismissal


def reply(body, in_reply_to=1234):
    comment = {"body": body, "user": {"login": "someone"}}
    if in_reply_to is not None:
        comment["in_reply_to_id"] = in_reply_to
    return {"comment": comment}


def test_recognises_the_trigger_phrases():
    assert dismissal.is_dismissal(reply("@quorum wontfix — guarded upstream"))
    assert dismissal.is_dismissal(reply("@quorum false positive"))
    assert dismissal.is_dismissal(reply("@quorum 誤検知です。上流で検証済み"))


def test_is_case_insensitive():
    assert dismissal.is_dismissal(reply("@Quorum WontFix"))


def test_a_top_level_comment_is_not_a_dismissal():
    """Without a reply target there is no finding being named."""
    assert not dismissal.is_dismissal(reply("@quorum wontfix", in_reply_to=None))


def test_an_ordinary_reply_is_not_a_dismissal():
    assert not dismissal.is_dismissal(reply("good catch, fixing now"))
    assert not dismissal.is_dismissal(reply("@quorum /review"))


def test_an_event_without_a_comment_is_not_a_dismissal():
    assert not dismissal.is_dismissal({})
    assert not dismissal.is_dismissal({"comment": None})


def test_the_reason_survives_without_the_trigger():
    assert (
        dismissal.extract_reason("@quorum wontfix — the caller already validates this")
        == "the caller already validates this"
    )
    assert (
        dismissal.extract_reason("@quorum 誤検知: 上流で検証済み") == "上流で検証済み"
    )


def test_a_bare_trigger_still_records_something():
    assert dismissal.extract_reason("@quorum wontfix") == "no reason given"
