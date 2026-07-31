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
