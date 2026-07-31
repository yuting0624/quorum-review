from quorum_review import diffs

DIFF = """\
diff --git a/app/search.py b/app/search.py
index 111..222 100644
--- a/app/search.py
+++ b/app/search.py
@@ -1,3 +1,4 @@
 import os
+query = "SELECT 1"
diff --git a/assets/logo.png b/assets/logo.png
index 333..444 100644
GIT binary patch
literal 12345
diff --git a/app/auth.py b/app/auth.py
index 555..666 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,2 +1,2 @@
-import hmac
+import hashlib
"""


def test_split_by_file_uses_the_post_change_path():
    sections = diffs.split_by_file(DIFF)
    assert set(sections) == {"app/search.py", "assets/logo.png", "app/auth.py"}
    assert "SELECT 1" in sections["app/search.py"]


def test_for_file_returns_only_that_section():
    section = diffs.for_file(DIFF, "app/auth.py")
    assert "import hashlib" in section
    assert "SELECT 1" not in section


def test_for_file_on_an_unknown_path_is_empty():
    assert diffs.for_file(DIFF, "app/nope.py") == ""


def test_truncate_drops_binary_patches():
    trimmed_diff, trimmed, _dropped = diffs.truncate(DIFF)
    assert "GIT binary patch" not in trimmed_diff
    assert "assets/logo.png" in trimmed


def test_truncate_caps_a_large_file_and_reports_it():
    big = "diff --git a/big.py b/big.py\n" + ("+x\n" * 20_000)
    trimmed_diff, trimmed, _dropped = diffs.truncate(big, file_char_limit=1_000)
    assert "big.py" in trimmed
    assert "truncated" in trimmed_diff
    assert len(trimmed_diff) < 2_000


def test_truncate_leaves_a_normal_diff_alone():
    trimmed_diff, trimmed, _dropped = diffs.truncate(
        "diff --git a/a.py b/a.py\n+print(1)\n"
    )
    assert trimmed == []
    assert "print(1)" in trimmed_diff


# -- the whole-diff budget -------------------------------------------------


def _diff_of(sizes: dict[str, int]) -> str:
    """A synthetic diff where each file's section is roughly `size` characters."""
    parts = []
    for path, size in sizes.items():
        body = "".join(f"+{'x' * 60}\n" for _ in range(max(1, size // 62)))
        parts.append(f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}")
    return "".join(parts)


def test_a_diff_inside_the_budget_keeps_everything():
    diff = _diff_of({"a.py": 500, "b.py": 500})
    _body, _trimmed, dropped = diffs.truncate(diff, total_char_limit=100_000)
    assert dropped == []


def test_files_that_do_not_fit_are_dropped_and_named():
    diff = _diff_of({"small.py": 200, "huge.py": 20_000})
    body, _trimmed, dropped = diffs.truncate(diff, total_char_limit=2_000)
    assert dropped == ["huge.py"]
    assert "small.py" in body
    assert "huge.py" not in body


def test_the_budget_keeps_the_most_files_not_the_first_ones():
    """Dropping six reviewable files to fit one unreviewable one is a bad trade.

    A five-hundred-line reformat is rarely the interesting change in a diff
    that also touches several small files, so the big one goes first.
    """
    diff = _diff_of({"aaa_huge.py": 9_000, "b.py": 300, "c.py": 300, "d.py": 300})
    body, _trimmed, dropped = diffs.truncate(diff, total_char_limit=2_000)

    assert dropped == ["aaa_huge.py"]
    for path in ("b.py", "c.py", "d.py"):
        assert path in body


def test_one_oversized_file_is_still_reviewed_rather_than_nothing():
    """A single-file pull request over budget must not produce an empty review."""
    diff = _diff_of({"only.py": 50_000})
    body, _trimmed, dropped = diffs.truncate(diff, total_char_limit=1_000)
    assert dropped == []
    assert "only.py" in body


def test_what_survives_is_emitted_in_the_diffs_own_order():
    """Line numbers and hunk order have to match what the model is told."""
    diff = _diff_of({"z_small.py": 200, "a_medium.py": 800})
    body, _trimmed, _dropped = diffs.truncate(diff, total_char_limit=100_000)
    assert body.index("z_small.py") < body.index("a_medium.py")


def test_dropping_is_stable_across_runs():
    """The same pull request must not review different files each time."""
    diff = _diff_of({"a.py": 900, "b.py": 900, "c.py": 900})
    first = diffs.truncate(diff, total_char_limit=2_000)[2]
    second = diffs.truncate(diff, total_char_limit=2_000)[2]
    assert first == second


# -- renames ---------------------------------------------------------------

RENAMED = """diff --git a/app/old_name.py b/app/new_name.py
similarity index 92%
rename from app/old_name.py
rename to app/new_name.py
--- a/app/old_name.py
+++ b/app/new_name.py
@@ -1,2 +1,2 @@
-x = 1
+x = 2
diff --git a/app/plain.py b/app/plain.py
--- a/app/plain.py
+++ b/app/plain.py
@@ -1 +1 @@
-a
+b
"""


def test_a_rename_is_reported_old_to_new():
    assert diffs.renames(RENAMED) == {"app/old_name.py": "app/new_name.py"}


def test_a_diff_with_no_renames_reports_none():
    assert diffs.renames(DIFF) == {}


def test_the_section_is_still_keyed_on_the_new_path():
    """Line numbers in a finding refer to the file after the change."""
    assert "app/new_name.py" in diffs.split_by_file(RENAMED)
    assert "app/old_name.py" not in diffs.split_by_file(RENAMED)


def test_a_rename_with_no_content_change_is_still_seen():
    """git emits no hunks for a pure move, and those are the ones where the
    findings certainly all survive."""
    pure = (
        "diff --git a/a.py b/b.py\n"
        "similarity index 100%\n"
        "rename from a.py\n"
        "rename to b.py\n"
    )
    assert diffs.renames(pure) == {"a.py": "b.py"}


def test_several_renames_in_one_diff():
    two = RENAMED + (
        "diff --git a/x.py b/y.py\nrename from x.py\nrename to y.py\n"
    )
    assert diffs.renames(two) == {
        "app/old_name.py": "app/new_name.py",
        "x.py": "y.py",
    }


def test_a_stray_rename_line_inside_a_hunk_is_not_a_rename():
    """A diff that adds the text 'rename from ...' to a file is a content
    change, and the header resets the state so it cannot be mistaken for one."""
    contrived = (
        "diff --git a/doc.md b/doc.md\n"
        "--- a/doc.md\n"
        "+++ b/doc.md\n"
        "@@ -1 +1,2 @@\n"
        " intro\n"
        "+rename to somewhere.py\n"
    )
    assert diffs.renames(contrived) == {}


# -- telling a binary file from a file that talks about them ----------------


def test_a_real_binary_patch_is_detected():
    assert diffs.is_binary(
        "diff --git a/logo.png b/logo.png\nGIT binary patch\nliteral 120\n"
    )


def test_the_short_form_is_detected():
    assert diffs.is_binary(
        "diff --git a/x.bin b/x.bin\nBinary files a/x.bin and b/x.bin differ\n"
    )


def test_source_code_mentioning_the_markers_is_not_binary():
    """How this bug was found: `diffs.py` was silently dropped from its own
    review, and the summary reported it as too large to send whole.

    A substring search matched a context line of source — the body of
    `is_binary` itself. Any file with either marker in a string, a docstring or
    a test fixture was excluded from review the same way, in any repository.
    """
    section = (
        "diff --git a/quorum_review/diffs.py b/quorum_review/diffs.py\n"
        "--- a/quorum_review/diffs.py\n"
        "+++ b/quorum_review/diffs.py\n"
        "@@ -40,3 +40,4 @@\n"
        " def is_binary(section: str) -> bool:\n"
        '     return "GIT binary patch" in section or "Binary files " in section\n'
        "+    # a change below it\n"
    )
    assert not diffs.is_binary(section)


def test_an_added_line_mentioning_a_marker_is_not_binary():
    """Documentation about binary diffs is still documentation."""
    section = (
        "diff --git a/docs/diffs.md b/docs/diffs.md\n"
        "@@ -1 +1,2 @@\n"
        " intro\n"
        '+git writes "Binary files a/x and b/x differ" for those.\n'
    )
    assert not diffs.is_binary(section)


def test_such_a_file_is_reviewed_rather_than_skipped():
    """The consequence, not just the predicate: it reached the model."""
    section = (
        "diff --git a/app/parser.py b/app/parser.py\n"
        "@@ -1 +1,2 @@\n"
        ' MARKERS = ("GIT binary patch",)\n'
        "+bug = True\n"
    )
    body, trimmed, _dropped = diffs.truncate(section)
    assert "app/parser.py" in body
    assert trimmed == []
