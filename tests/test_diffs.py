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
