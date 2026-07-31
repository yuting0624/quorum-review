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
    trimmed_diff, trimmed = diffs.truncate(DIFF)
    assert "GIT binary patch" not in trimmed_diff
    assert "assets/logo.png" in trimmed


def test_truncate_caps_a_large_file_and_reports_it():
    big = "diff --git a/big.py b/big.py\n" + ("+x\n" * 20_000)
    trimmed_diff, trimmed = diffs.truncate(big, file_char_limit=1_000)
    assert "big.py" in trimmed
    assert "truncated" in trimmed_diff
    assert len(trimmed_diff) < 2_000


def test_truncate_leaves_a_normal_diff_alone():
    trimmed_diff, trimmed = diffs.truncate(
        "diff --git a/a.py b/a.py\n+print(1)\n"
    )
    assert trimmed == []
    assert "print(1)" in trimmed_diff
