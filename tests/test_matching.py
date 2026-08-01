"""Regression tests for defect matching.

Every case below is real output from a live run against the benchmark pull
request. They are kept verbatim because the matcher's two thresholds were tuned
against exactly these, and a change that breaks one of them is a change that
either re-posts duplicate comments or silently drops a finding.
"""

import pytest

from quorum_review.matching import Report, normalize_snippet, same_defect, title_tokens

SHARING = "app/sharing.py"
EXPORT = "app/export.py"
ADMIN = "app/admin.py"
SEARCH = "app/search.py"


def r(path, line, title, snippet=None):
    """Build a report.

    The snippet defaults to something unique per line, because real findings
    quote the code they are about. A shared placeholder would make the
    snippet-overlap rule fire everywhere and quietly invalidate these tests.
    """
    return Report(path, line, snippet or f"value_{line} = compute_{line}()", title)


# -- normalisation ---------------------------------------------------------


def test_normalize_ignores_layout_and_comments():
    assert normalize_snippet("  x = 1  # note\n") == normalize_snippet("x = 1")


def test_stemming_unifies_plurals_and_word_forms():
    assert title_tokens("share links") & title_tokens("a share link")
    # 'resolving' and 'resolution' have to collide or the two ways of naming
    # one defect never match.
    assert title_tokens("resolving") == title_tokens("resolution")


def test_stemming_unifies_a_word_with_its_own_plural():
    """Truncating before stripping the 's' is what makes this hold."""
    assert title_tokens("class") == title_tokens("classes")


# -- must merge: one defect, reported two ways -----------------------------

SAME = [
    (
        "expiry, anchored 6 lines apart",
        r(SHARING, 40, "Expiration check missing when resolving share links"),
        r(SHARING, 34, "Share link expiration is not enforced during resolution"),
    ),
    (
        "expiry, anchored 16 lines apart and worded very differently",
        r(SHARING, 40, "Expiration check missing when resolving share links"),
        r(
            SHARING,
            24,
            "Share links never expire: TTL stored as a duration and never checked",
        ),
    ),
    (
        "TOCTOU, one model naming the write and the other naming open()",
        r(EXPORT, 23, "TOCTOU between existence check and file write in export_document"),
        r(EXPORT, 27, "TOCTOU between existence check and open() in export_document"),
    ),
]


@pytest.mark.parametrize("label,a,b", SAME, ids=[case[0] for case in SAME])
def test_the_same_defect_matches(label, a, b):
    assert same_defect(a, b)
    assert same_defect(b, a)  # order must not matter


# -- must not merge: distinct defects, often in the same file --------------

DISTINCT = [
    (
        "missing authz vs swallowed authz, 9 lines apart",
        r(ADMIN, 18, "delete_document performs no admin or ownership check"),
        r(ADMIN, 27, "purge_user swallows the admin authorization failure"),
    ),
    (
        "two different bugs in the same function",
        r(ADMIN, 27, "purge_user swallows the admin authorization failure"),
        r(ADMIN, 31, "purge_user deletes orphaned shares belonging to all users"),
    ),
    (
        "path traversal vs TOCTOU in the same file",
        r(EXPORT, 15, "Path traversal: export filename joined without validation"),
        r(EXPORT, 23, "TOCTOU between existence check and file write in export_document"),
    ),
    (
        "timing attack vs mutable default",
        r(SHARING, 37, "Share signature compared with non-constant-time =="),
        r(SHARING, 12, "Mutable default argument accumulates scopes across calls"),
    ),
    (
        "adjacent lines, unrelated defects",
        r(SHARING, 37, "Share signature compared with non-constant-time =="),
        r(SHARING, 40, "Expiration check missing when resolving share links"),
    ),
    (
        "same wording, different endpoint — the floor on shared words earns its keep",
        r(ADMIN, 10, "Missing authorization check on delete"),
        r(ADMIN, 40, "Missing authorization check on purge"),
    ),
    (
        "same class of bug, different function",
        r(SEARCH, 15, "SQL injection in search"),
        r(SEARCH, 60, "SQL injection in suggest"),
    ),
]


@pytest.mark.parametrize("label,a,b", DISTINCT, ids=[case[0] for case in DISTINCT])
def test_distinct_defects_do_not_match(label, a, b):
    assert not same_defect(a, b)
    assert not same_defect(b, a)


# -- structural ------------------------------------------------------------


def test_a_different_file_is_never_a_match():
    assert not same_defect(
        r("app/a.py", 10, "SQL injection via f-string interpolation of the term"),
        r("app/b.py", 10, "SQL injection via f-string interpolation of the term"),
    )


def test_identical_code_far_apart_does_not_match_on_position():
    """A file can contain the same `except Exception: pass` twice.

    Quoted code widens the positional window; it never removes it, so two
    instances of one pattern stay separate as long as they are described
    differently.
    """
    swallow = "except Exception: pass"
    assert not same_defect(
        r(ADMIN, 10, "Retry loop hides a connection failure", swallow),
        r(ADMIN, 90, "Cleanup path swallows a permission error", swallow),
    )


def test_known_limitation_two_instances_described_identically_do_merge():
    """A real gap, asserted so it is not mistaken for correct behaviour.

    Two separate occurrences of one pattern, given the same title by the model,
    are indistinguishable to this matcher and collapse into one finding — the
    second is lost. Fixing it needs a signal the models do not currently give
    us; documented in the README as a known limitation.
    """
    assert same_defect(
        r(ADMIN, 10, "Swallowed exception hides an authorization failure"),
        r(ADMIN, 90, "Swallowed exception hides an authorization failure"),
    )


def test_adjacent_lines_match_without_needing_the_title():
    assert same_defect(r(ADMIN, 10, "one thing"), r(ADMIN, 11, "another thing"))


# -- what the tokeniser cannot do -------------------------------------------


def test_a_japanese_title_yields_no_tokens():
    """`review-language` is an input, so this is a configuration people use,
    not a hypothetical. Pinned so the limitation is known rather than
    discovered: for a language this cannot tokenise, duplicate suppression is
    position-only."""
    from quorum_review.matching import title_tokens

    assert title_tokens("共有リンクの有効期限が検証されていない") == set()


def test_identifiers_survive_whatever_the_prose_is():
    """A title that quotes code still tokenises, because identifiers are Latin
    whatever the language around them. Partial coverage, and worth knowing
    before concluding the matcher does nothing outside English.

    Stemmed to five characters like every other word, so `share_token` arrives
    as `share` — which is enough to collide with another report naming the same
    thing, and is all this is for.
    """
    from quorum_review.matching import title_tokens

    assert title_tokens("`share_token` の有効期限が検証されていない") == {"share"}
    assert title_tokens("`validate_export_name` が呼ばれていない") == {"valid"}


def test_two_japanese_titles_fall_back_to_position():
    """Not merged on wording — and that is the safe direction. Character
    bigrams would merge these, and would also merge two titles differing only
    in `削除` versus `パージ`, which are different bugs."""
    from quorum_review.matching import Report, same_defect

    a = Report("app/sharing.py", 10, "a = 1", "共有リンクの有効期限が検証されていない")
    b = Report("app/sharing.py", 40, "b = 2", "共有リンクの期限チェックが欠落している")

    assert not same_defect(a, b)

    near = Report("app/sharing.py", 11, "c = 3", "共有リンクの期限チェックが欠落している")
    assert same_defect(a, near)


def test_the_english_path_is_unchanged():
    """The documentation above must not have moved the behaviour it describes."""
    from quorum_review.matching import Report, same_defect

    a = Report("app/sharing.py", 10, "a = 1", "Share link expiry is never checked")
    b = Report("app/sharing.py", 40, "b = 2", "Share link expiry check missing on read")

    assert same_defect(a, b)


# -- the phrase the reviewer answers to -------------------------------------


def test_the_default_mention(monkeypatch):
    from quorum_review.matching import mention

    monkeypatch.delenv("QUORUM_TRIGGER", raising=False)
    assert mention() == "@quorum"


def test_a_configured_mention_reaches_the_dismissal_triggers(monkeypatch):
    """Two reviewers in one repository is the case this exists for. If the
    mention were configurable and the dismissal phrases were not, `@bot
    wontfix` would be recognised as a question instead."""
    from quorum_review.matching import dismissal_triggers, is_dismissal_text

    monkeypatch.setenv("QUORUM_TRIGGER", "@reviewer2")

    assert dismissal_triggers() == (
        "@reviewer2 wontfix",
        "@reviewer2 false positive",
        "@reviewer2 誤検知",
    )
    assert is_dismissal_text("@reviewer2 wontfix — guarded upstream")
    assert not is_dismissal_text("@quorum wontfix — guarded upstream")


def test_an_empty_setting_falls_back(monkeypatch):
    """An action input left unset arrives as "". A trigger that matches every
    comment would turn every comment on the repository into a model call."""
    from quorum_review.matching import mention

    monkeypatch.setenv("QUORUM_TRIGGER", "")
    assert mention() == "@quorum"

    monkeypatch.setenv("QUORUM_TRIGGER", "   ")
    assert mention() == "@quorum"


def test_a_typo_with_an_obvious_intent_is_collapsed(monkeypatch):
    from quorum_review.matching import mention

    monkeypatch.setenv("QUORUM_TRIGGER", "@ quorum ")
    assert mention() == "@ quorum"


def test_the_trigger_is_read_at_call_time(monkeypatch):
    """Not at import. A value bound when the module loaded would freeze
    whatever the environment said then, which is how the action's inputs would
    fail to reach it."""
    from quorum_review.matching import mention

    monkeypatch.setenv("QUORUM_TRIGGER", "@one")
    assert mention() == "@one"
    monkeypatch.setenv("QUORUM_TRIGGER", "@two")
    assert mention() == "@two"
