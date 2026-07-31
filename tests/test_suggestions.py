"""Suggestions are applied by a click, so the guards around them get tests.

A wrong suggestion is worse than none: it lands in the branch without anyone
reading it closely.
"""

from src import report as report_mod
from src.schema import MAX_FIX_LINES, Finding, findings_from_payload

BASE = {
    "file_path": "app/auth.py",
    "line": 20,
    "category": "security",
    "severity": "high",
    "title": "Token compared with ==",
    "body": "Use a constant-time comparison.",
    "code_snippet": "if token == expected:",
}


def payload(**overrides):
    return {"findings": [{**BASE, **overrides}]}


def only(**overrides) -> Finding:
    return findings_from_payload(payload(**overrides), "m")[0]


def test_a_single_line_fix_is_kept():
    finding = only(fix_end_line=20, fix_replacement="    if compare_digest(a, b):")
    assert finding.fix_replacement
    assert finding.fix_end_line == 20


def test_a_multi_line_fix_is_kept():
    finding = only(fix_end_line=23, fix_replacement="a\nb\nc\nd")
    assert finding.fix_end_line == 23


def test_no_fix_offered_is_fine():
    finding = only(fix_end_line=0, fix_replacement="")
    assert finding.fix_replacement == ""


def test_a_range_ending_before_it_starts_is_discarded():
    finding = only(fix_end_line=5, fix_replacement="something")
    assert finding.fix_replacement == ""
    assert finding.fix_end_line == 0


def test_a_range_larger_than_a_review_comment_is_discarded():
    """Past a point it is a refactor, and nobody reads a refactor before
    clicking apply."""
    finding = only(fix_end_line=20 + MAX_FIX_LINES + 1, fix_replacement="x")
    assert finding.fix_replacement == ""


def test_a_missing_fix_field_does_not_break_parsing():
    """Older prompts, or a model that omits the field, must still work."""
    item = dict(BASE)
    finding = findings_from_payload({"findings": [item]}, "m")[0]
    assert finding.fix_replacement == ""
    assert finding.fix_end_line == 0


def test_the_comment_renders_a_suggestion_block():
    finding = only(fix_end_line=20, fix_replacement="    if compare_digest(a, b):")
    body = report_mod.render_inline(finding)
    assert "```suggestion" in body
    assert "compare_digest" in body


def test_the_suggestion_can_be_dropped_while_keeping_the_finding():
    """Used when GitHub rejects the range the suggestion needs."""
    finding = only(fix_end_line=23, fix_replacement="a\nb")
    body = report_mod.render_inline(finding, with_suggestion=False)
    assert "```suggestion" not in body
    assert finding.title in body
    assert finding.body in body


def test_no_suggestion_block_when_no_fix_was_offered():
    assert "```suggestion" not in report_mod.render_inline(only())
