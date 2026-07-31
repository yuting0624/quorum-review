"""Findings are model output, and they go straight into Markdown.

The summary table is where the project makes its argument — the Evidence column
is the only place a reader learns that two models agreed without seeing each
other. A cell that breaks the table takes that with it, and it breaks silently:
GitHub renders something, just not what was meant.
"""

from __future__ import annotations

import pytest

from quorum_review.report import (
    MAX_CELL_CHARS,
    RunReport,
    _row,
    _table,
    cell,
    flatten,
    render,
    render_inline,
)
from quorum_review.schema import Finding

COLUMNS = 5


def finding(title="something is wrong", **kwargs) -> Finding:
    base = {
        "file_path": "app/x.py",
        "line": 3,
        "category": "security",
        "severity": "high",
        "title": title,
        "body": "because of reasons",
        "code_snippet": "bad = 1",
        "finding_id": "abc",
        "reported_by": ["gemini-3.6-flash"],
    }
    base.update(kwargs)
    return Finding(**base)


def structural_pipes(row: str) -> int:
    r"""Count the pipes that make columns, the way Markdown resolves them.

    Escaped backslashes are collapsed first, then escaped pipes removed. The
    other order would hide exactly the bug this file exists to prevent:
    `a\|b` escaped naively becomes `a\\|b`, which Markdown reads as a literal
    backslash followed by a live pipe.
    """
    return row.replace("\\\\", "").replace("\\|", "").count("|")


# -- the table stays a table -----------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Unsafe shell: cmd | grep secret",
        r"The regex \| is an escaped pipe, not alternation",
        r"Windows path C:\temp\x | and a pipe",
        "Regex alternation (a|b|c) is unanchored",
        "Short-circuit a || b masks the error",
        "|leading pipe",
        "trailing pipe|",
    ],
)
def test_a_pipe_in_a_title_does_not_add_a_column(title: str):
    """A security reviewer's titles contain pipes constantly.

    Unescaped, GitHub shifts every later cell left, and the Evidence column —
    the whole argument the table exists to make — shows a fragment of the
    title instead.
    """
    row = _row(finding(title))
    assert structural_pipes(row) == COLUMNS + 1
    assert "unchecked" in row  # the last column survived


def test_a_newline_in_a_title_does_not_end_the_table():
    """Worse than a pipe: the rest of the summary renders as prose."""
    row = _row(finding("Line one\nline two"))
    assert "\n" not in row
    assert "Line one line two" in row


def test_a_pipe_in_a_path_is_escaped_too():
    row = _row(finding(file_path="app/we|rd.py"))
    assert structural_pipes(row) == COLUMNS + 1


def test_a_pipe_in_a_model_name_is_escaped():
    """Model IDs come from configuration, which is also not ours to trust."""
    row = _row(finding(reported_by=["odd|name"]))
    assert structural_pipes(row) == COLUMNS + 1


def test_every_row_of_a_table_has_the_same_shape():
    table = _table([finding("a | b"), finding("plain"), finding("c\nd")])
    rows = table.splitlines()[2:]
    assert rows
    assert {structural_pipes(row) for row in rows} == {COLUMNS + 1}


def test_the_whole_summary_survives_a_hostile_title():
    body = render(RunReport(confirmed=[finding("cmd | grep\nsecond line")]))
    header = "| Severity | Category | Location | Finding | Evidence |"
    assert header in body
    row = next(
        line for line in body.splitlines() if "cmd" in line and line.startswith("|")
    )
    assert structural_pipes(row) == COLUMNS + 1


# -- bounds ----------------------------------------------------------------


def test_a_very_long_title_is_truncated():
    """The schema asks for 80 characters. A run that returns a paragraph would
    make one row unreadable and push the rest off the screen."""
    rendered = cell("x" * 500)
    assert len(rendered) == MAX_CELL_CHARS
    assert rendered.endswith("…")


def test_a_title_at_the_limit_is_untouched():
    exact = "y" * MAX_CELL_CHARS
    assert cell(exact) == exact


def test_empty_and_missing_values_are_safe():
    assert cell("") == ""
    assert flatten(None) == ""  # type: ignore[arg-type]


# -- outside tables --------------------------------------------------------


def test_a_newline_does_not_break_bold_in_an_inline_comment():
    rendered = render_inline(finding("Line one\nline two"))
    assert "**Line one line two**" in rendered


def test_a_pipe_is_left_alone_outside_a_table():
    """Escaping it there would show a backslash to the reader for nothing."""
    rendered = render_inline(finding("cmd | grep"))
    assert "**cmd | grep**" in rendered


def test_refuted_findings_are_flattened_in_their_list():
    report = RunReport(
        verification_on=True,
        refuted=[finding("Line one\nline two", verifier_reason="no\nreason")],
    )
    body = render(report)
    assert "**Line one line two**" in body
    assert "- **Line one\n" not in body


def test_a_backslash_before_a_pipe_does_not_survive_as_a_column_break():
    r"""The bug the reviewer found in the first version of this escaping.

    Escaping only the pipe turns `a\|b` into `a\\|b`, which Markdown reads as
    a literal backslash followed by a live pipe — precisely the break the
    escaping was added to prevent. Backslashes have to go first.

    And `\|` is not exotic: it is how an escaped pipe is written in a regular
    expression, which is a thing findings quote.
    """
    rendered = cell(r"regex \| alternation")
    assert rendered == "regex " + "\\\\" + "\\|" + " alternation"
    assert structural_pipes(f"| {rendered} |") == 2


def test_a_lone_backslash_is_escaped():
    assert cell("\\") == "\\\\"


# -- suppression says what it suppressed ------------------------------------


def test_suppressed_findings_are_listed_not_just_counted():
    """Matching is positional and by wording, so it can be wrong.

    A summary that reports only a count gives a reader no way to notice a bad
    match — which is exactly the position this project was in when reading its
    own re-review.
    """
    from quorum_review.report import RunReport, render

    body = render(
        RunReport(
            suppressed=2,
            suppressed_titles=["SQL injection in search", "Missing owner check"],
        )
    )
    assert "2 finding(s) were already reported" in body
    assert "<summary>Which ones</summary>" in body
    assert "- SQL injection in search" in body
    assert "- Missing owner check" in body


def test_the_list_is_collapsed():
    """The point of suppression is that these are not worth reading twice."""
    from quorum_review.report import RunReport, render

    body = render(RunReport(suppressed=1, suppressed_titles=["a"]))
    assert "<details>" in body


def test_nothing_is_listed_when_nothing_was_suppressed():
    from quorum_review.report import RunReport, render

    assert "Which ones" not in render(RunReport(confirmed=[finding()]))


# -- HTML, because several sections are <details> ---------------------------


def test_a_title_cannot_close_a_details_block():
    """Reported by the reviewer on the pull request that added the list.

    Several sections wrap their content in `<details>`, and GitHub renders raw
    HTML in comments. A title is model output derived from a diff someone else
    wrote, so `</details>` in one would close the block early and spill the
    rest of the summary out of it.
    """
    from quorum_review.report import RunReport, render

    body = render(
        RunReport(suppressed=1, suppressed_titles=["</details><h1>gotcha"])
    )
    assert "</details><h1>" not in body
    assert "&lt;/details&gt;" in body
    # The block this is inside still closes exactly once, where it should.
    assert body.count("<summary>Which ones</summary>") == 1


def test_escaping_is_invisible_to_the_reader():
    """`&lt;` renders as `<`, so a legitimate comparison still reads right."""
    assert flatten("a < b && c > d") == "a &lt; b &amp;&amp; c &gt; d"


def test_the_ampersand_is_escaped_first():
    """The other order turns `<` into `&amp;lt;`, which the reader then sees."""
    assert flatten("<") == "&lt;"
    assert "&amp;lt;" not in flatten("<")


def test_a_refuted_finding_cannot_break_out_of_its_details_block():
    from quorum_review.report import RunReport, render

    body = render(
        RunReport(
            verification_on=True,
            refuted=[finding("</details>escaped", verifier_reason="</details>too")],
        )
    )
    assert "</details>escaped" not in body
    assert "</details>too" not in body


# -- the summary shares a size limit with the ledger ------------------------


def test_the_suppressed_list_is_bounded():
    """The visible body and the ledger marker share GitHub's 65,536-character
    limit. Overflowing makes `fit_to_comment` drop the ledger's history to make
    room for a list nobody reads."""
    from quorum_review.report import MAX_SUPPRESSED_LISTED, RunReport, render

    many = [f"finding number {n}" for n in range(MAX_SUPPRESSED_LISTED + 15)]
    body = render(RunReport(suppressed=len(many), suppressed_titles=many))

    assert body.count("\n- finding number") == MAX_SUPPRESSED_LISTED
    assert "…and 15 more" in body
    assert f"{len(many)} finding(s) were already reported" in body


def test_a_short_list_says_nothing_about_more():
    from quorum_review.report import RunReport, render

    body = render(RunReport(suppressed=2, suppressed_titles=["a", "b"]))
    assert "more" not in body.split("<summary>Which ones</summary>")[1][:200]


def test_escaping_cannot_grow_a_value_past_the_limit():
    """Reported by the reviewer: truncation ran before escaping.

    A title of 160 ampersands passed the length check and then grew to 800
    characters. Twenty-five of those is a fifth of GitHub's comment budget
    spent on nothing.
    """
    for hostile in ("&" * 400, "<" * 400, "&<>" * 200):
        assert len(flatten(hostile)) <= MAX_CELL_CHARS


def test_a_truncated_entity_is_not_left_half_written():
    """The cost of escaping first: a cut can land inside `&amp;`, and `&am`
    renders as literal text rather than an ampersand."""
    result = flatten("&" * 400).removesuffix("…")
    assert not result.endswith(("&", "&a", "&am", "&amp"))
    assert result.endswith("&amp;")


def test_a_value_under_the_limit_keeps_its_whole_text():
    assert flatten("a & b") == "a &amp; b"


@pytest.mark.parametrize(
    "hostile",
    ["&" * 400, "<" * 400, "|" * 400, "\\" * 400, "a|" + chr(92) + "&<" * 100],
)
def test_a_cell_stays_within_the_limit_after_every_escape(hostile: str):
    """Reported by the reviewer: the bound was asserted on `flatten`, and
    `cell` escaped backslashes and pipes afterwards — doubling a hostile value
    straight back over it. Escaping is not length-preserving, so the cut has
    to come last."""
    assert len(cell(hostile)) <= MAX_CELL_CHARS


@pytest.mark.parametrize(
    "hostile",
    ["&" * 400, "<" * 400, "|" * 400, "\\" * 400, "a|" + chr(92) + "&<" * 100],
)
def test_a_truncated_cell_still_makes_one_row(hostile: str):
    """The subtle half: a cut landing after an odd number of backslashes
    escapes the *closing* pipe, and the column disappears."""
    assert structural_pipes(f"| {cell(hostile)} |") == 2


def test_a_truncated_cell_never_ends_mid_escape():
    for hostile in ("&" * 400, "\\" * 400):
        body = cell(hostile).removesuffix("…")
        assert not body.endswith(("&", "&a", "&am", "&amp"))
        assert (len(body) - len(body.rstrip("\\"))) % 2 == 0


def test_a_cut_landing_on_a_backslash_before_whitespace_is_repaired():
    r"""Reported by the reviewer: the parity check ran before `rstrip()`.

    A cut ending `\   ` looks even — the last character is a space, so no
    trailing backslashes are counted — and then stripping the whitespace puts
    an odd backslash back on the end, where it escapes whatever follows.
    """
    from quorum_review.report import _bound

    backslash = chr(92)
    probe = ("x" * 155) + backslash + "   yyyy"
    body = _bound(probe, 160).removesuffix("…")

    assert (len(body) - len(body.rstrip(backslash))) % 2 == 0
    assert not body.endswith(" ")


def test_the_repair_leaves_an_even_run_alone():
    from quorum_review.report import _bound

    backslash = chr(92)
    # Long enough that the cut actually happens, with the run just inside it.
    probe = ("x" * 153) + (backslash * 4) + "   " + ("z" * 40)
    body = _bound(probe, 160).removesuffix("…")

    assert len(probe) > 160
    assert body.endswith(backslash * 4)


@pytest.mark.parametrize(
    "probe",
    [
        ("x" * 155) + chr(92) + "   " + "y" * 40,
        ("x" * 153) + " " + chr(92) + "  " + "y" * 40,
        ("x" * 152) + "&amp" + "  " + "y" * 40,
        ("x" * 153) + chr(92) * 4 + "   " + "z" * 40,
        ("x" * 150) + " " + chr(92) + " " + chr(92) + " " + "y" * 40,
    ],
)
def test_the_repairs_reach_a_fixed_point(probe: str):
    """They expose each other, which is why `_bound` loops.

    Stripping whitespace can uncover a backslash that was even a moment ago;
    dropping that backslash can uncover whitespace again. Two attempts to
    sequence them by hand were each wrong in a different direction.
    """
    from quorum_review.report import _bound

    body = _bound(probe, 160).removesuffix("…")

    assert not body.endswith(" ")
    assert (len(body) - len(body.rstrip(chr(92)))) % 2 == 0
    assert not body.endswith(("&", "&a", "&am", "&amp"))
