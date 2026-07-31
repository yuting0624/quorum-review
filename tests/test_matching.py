"""Regression tests for defect matching.

Every case below is real output from a live run against the benchmark pull
request. They are kept verbatim because the matcher's two thresholds were tuned
against exactly these, and a change that breaks one of them is a change that
either re-posts duplicate comments or silently drops a finding.
"""

import pytest

from src.matching import Report, normalize_snippet, same_defect, title_tokens

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
