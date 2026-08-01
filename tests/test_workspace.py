"""The toolbox is reachable from attacker-controlled text, so its limits are asserted.

Anyone who can open a pull request controls the diff, and the diff is what the
model reads before deciding which tools to call. Every boundary here — the
checkout root, the exclusion list, the budget — is therefore a boundary against
input someone else wrote, not merely a guard against a confused model.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

from quorum_review.pathfilter import PathFilter
from quorum_review.workspace import (
    MAX_MATCHES,
    Workspace,
    checkout_has_commit,
    workspace_root,
)


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "permissions.py").write_text(
        "REQUIRED_SCOPES = {\n    'document.read': 'read',\n}\n\n"
        "def has_scope(user, action):\n    return True\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "reports.py").write_text(
        "import permissions\n\n\ndef write_report(user):\n"
        "    return permissions.has_scope(user, 'document.report')\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=hunter2\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "left-pad.js").write_text("//\n", encoding="utf-8")
    # Not a .png: that extension is already excluded, which would test the
    # filter rather than the binary check.
    (tmp_path / "app" / "index.dat").write_bytes(b"\x00\x01\x02binary\x00")
    return tmp_path


@pytest.fixture
def space(repo: pathlib.Path) -> Workspace:
    return Workspace(repo, PathFilter())


# -- reading ---------------------------------------------------------------


def test_read_file_returns_numbered_lines(space: Workspace):
    output = space.read_file("app/permissions.py")
    assert "REQUIRED_SCOPES" in output
    assert "    1  " in output
    assert "lines 1-6 of 6" in output


def test_a_window_past_the_end_says_so_instead_of_returning_nothing(space: Workspace):
    assert "past the end" in space.read_file("app/permissions.py", start_line=900)


def test_a_partial_window_reports_what_was_left(space: Workspace):
    output = space.read_file("app/permissions.py", start_line=1, line_count=2)
    assert "... (4 more lines)" in output


def test_reading_records_the_file_for_the_summary(space: Workspace):
    space.read_file("app/permissions.py")
    space.read_file("app/permissions.py")
    assert space.files_read == ["app/permissions.py", "app/permissions.py"]
    assert "`app/permissions.py`" in space.summary()


def test_a_search_that_opens_nothing_is_described_as_such(space: Workspace):
    space.run("search", {"pattern": "nothing-matches-this"})
    assert space.summary() == "1 repository searches, no files opened"


def test_binary_files_are_refused_rather_than_returned_as_mojibake(space: Workspace):
    assert "binary" in space.run("read_file", {"path": "app/index.dat"})


# -- confinement -----------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "../outside.txt",
        "app/../../outside.txt",
        "/etc/passwd",
        "..\\..\\outside.txt",
    ],
)
def test_paths_outside_the_checkout_are_refused(space: Workspace, path: str):
    result = space.run("read_file", {"path": path})
    assert result.startswith("Error:")
    assert "outside the repository" in result or "not a file" in result


def test_a_symlink_pointing_out_of_the_checkout_is_refused(
    repo: pathlib.Path, tmp_path_factory
):
    """Resolution happens before the containment check, which is why this works."""
    outside = tmp_path_factory.mktemp("elsewhere") / "secret.txt"
    outside.write_text("private\n", encoding="utf-8")
    link = repo / "shortcut.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks unprivileged")

    result = Workspace(repo, PathFilter()).run("read_file", {"path": "shortcut.txt"})
    assert "outside the repository" in result
    assert "private" not in result


def test_dotenv_is_never_readable_even_with_no_exclusions_configured(
    repo: pathlib.Path,
):
    space = Workspace(repo, PathFilter(use_defaults=False))
    assert "hunter2" not in space.run("read_file", {"path": ".env"})


def test_the_git_directory_is_never_readable(repo: pathlib.Path):
    (repo / ".git").mkdir()
    (repo / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    space = Workspace(repo, PathFilter(use_defaults=False))
    assert space.run("read_file", {"path": ".git/config"}).startswith("Error:")


def test_the_review_exclusions_also_apply_to_the_tools(space: Workspace):
    """A budget spent reading node_modules is a budget not spent on the code."""
    assert space.run("read_file", {"path": "node_modules/left-pad.js"}).startswith(
        "Error:"
    )
    assert "node_modules" not in space.list_files("")


# -- search and listing ----------------------------------------------------


def test_search_reports_file_and_line(space: Workspace):
    output = space.search("def has_scope")
    assert "app/permissions.py:5:" in output


def test_search_can_be_narrowed_by_glob(space: Workspace):
    assert "No matches" in space.search("demo", path_glob="*.py")
    assert "README.md" in space.search("demo", path_glob="*.md")


def test_an_invalid_regular_expression_is_explained_not_raised(space: Workspace):
    result = space.run("search", {"pattern": "([unclosed"})
    assert result.startswith("Error:")
    assert "valid regular expression" in result


def test_search_stops_at_the_match_ceiling(tmp_path: pathlib.Path):
    body = "\n".join("needle" for _ in range(MAX_MATCHES * 2))
    (tmp_path / "big.txt").write_text(body, encoding="utf-8")
    output = Workspace(tmp_path, PathFilter()).search("needle")
    assert f"stopped at {MAX_MATCHES} matches" in output
    assert output.count("big.txt") == MAX_MATCHES


def test_list_files_marks_directories(space: Workspace):
    entries = space.list_files("").splitlines()
    assert "app/" in entries
    assert "README.md" in entries


# -- budget ----------------------------------------------------------------


def test_the_call_budget_is_enforced(repo: pathlib.Path):
    space = Workspace(repo, PathFilter(), max_calls=2)
    assert not space.run("read_file", {"path": "README.md"}).startswith("Tool budget")
    assert not space.run("read_file", {"path": "README.md"}).startswith("Tool budget")
    assert space.exhausted
    assert space.run("read_file", {"path": "README.md"}).startswith("Tool budget")


def test_an_exhausted_budget_answers_rather_than_going_silent(repo: pathlib.Path):
    """The model has to be told, or it waits for a result that will never come."""
    space = Workspace(repo, PathFilter(), max_calls=0)
    assert "produce your findings from what you have" in space.run("read_file", {})


def test_an_unknown_tool_is_reported_not_raised(space: Workspace):
    assert space.run("delete_everything", {"path": "."}).startswith("Error: unknown tool")


def test_nonsense_arguments_fall_back_to_defaults(space: Workspace):
    """Models pass strings where integers belong; that must not end the review."""
    output = space.run(
        "read_file", {"path": "app/permissions.py", "start_line": "two"}
    )
    assert "REQUIRED_SCOPES" in output


# -- knowing whether the checkout is the right one -------------------------


def test_a_checkout_containing_the_commit_is_recognised(tmp_path: pathlib.Path):
    sha = _git_repo_with_one_commit(tmp_path)
    assert checkout_has_commit(tmp_path, sha)


def test_a_checkout_without_the_commit_is_rejected(tmp_path: pathlib.Path):
    """An issue_comment run checks out the default branch, not the pull request."""
    _git_repo_with_one_commit(tmp_path)
    assert not checkout_has_commit(tmp_path, "0" * 40)


def test_no_sha_means_no_claim_of_a_match(tmp_path: pathlib.Path):
    assert not checkout_has_commit(tmp_path, "")


def test_a_directory_that_is_not_a_repository_is_rejected(tmp_path: pathlib.Path):
    assert not checkout_has_commit(tmp_path, "0" * 40)


def test_no_workspace_variable_means_no_workspace(monkeypatch):
    monkeypatch.delenv("GITHUB_WORKSPACE", raising=False)
    assert workspace_root() is None


def test_a_workspace_variable_pointing_nowhere_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path / "does-not-exist"))
    assert workspace_root() is None


def _git_repo_with_one_commit(root: pathlib.Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "test")
    (root / "file.txt").write_text("x\n", encoding="utf-8")
    git("add", "file.txt")
    git("commit", "-qm", "one")
    return git("rev-parse", "HEAD")


# -- this repository's own answer key --------------------------------------


def test_the_benchmark_answer_key_is_hidden_from_the_models():
    """Once the models could read the checkout, they found the answer key and opened it.

    `benchmark/seeded-bugs/README.md` lists every seeded bug and decoy by file;
    `tests/test_fixture_integrity.py` asserts each one. Both live on main, which
    keeps them out of PR #1's diff — enough while the reviewer could only read
    the diff, and not enough afterwards. They turned up in the tool logs of
    several measurement runs, and those numbers were discarded.

    Asserted because the failure is silent and flattering: the runs still
    complete, and the scores go *up*.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    assert (root / ".quorumignore").is_file()

    hidden = Workspace(root, PathFilter.build(root=root))
    for path in (
        "benchmark/seeded-bugs/README.md",
        "tests/test_fixture_integrity.py",
        "benchmark/runs/anything.json",
    ):
        assert hidden.run("read_file", {"path": path}).startswith("Error:")


# -- one policy file, resolved once ----------------------------------------


def test_the_tools_use_the_patterns_they_are_given_not_the_checkout(
    monkeypatch, repo: pathlib.Path
):
    """The gap the reviewer found in its own diff.

    `.quorumignore` was read twice: once by the diff selector, at whichever ref
    policy said to trust, and again here — from the checkout, which *is* the
    branch under review. On a fork that meant the base's copy governed the diff
    while the head's copy governed the tools. Passing the resolved set in is
    what makes the two halves the same decision.
    """
    (repo / ".quorumignore").write_text("app/**\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    monkeypatch.delenv("QUORUM_REPO_ACCESS", raising=False)

    from quorum_review import workspace as workspace_mod

    # The resolved set says nothing about app/, so the head's attempt to hide
    # it must not take effect.
    space = workspace_mod.build(1, 10, patterns=["*.lock"])[0]
    assert space is not None
    assert "REQUIRED_SCOPES" in space.run("read_file", {"path": "app/permissions.py"})


def test_no_resolved_patterns_falls_back_to_the_checkout(monkeypatch, repo):
    """A local run has no diff selector to resolve them."""
    (repo / ".quorumignore").write_text("app/**\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_WORKSPACE", str(repo))
    monkeypatch.delenv("QUORUM_REPO_ACCESS", raising=False)

    from quorum_review import workspace as workspace_mod

    space = workspace_mod.build(1, 10)[0]
    assert space is not None
    assert space.run("read_file", {"path": "app/permissions.py"}).startswith("Error:")


# -- which tree was actually read ------------------------------------------


def test_the_checkout_commit_is_reported(tmp_path: pathlib.Path):
    """`refs/pull/N/merge` is recomputed when the base branch moves, and it is
    resolved at checkout time rather than when the diff was fetched. The two
    can disagree, so the summary says which tree the models read."""
    sha = _git_repo_with_one_commit(tmp_path)
    from quorum_review.workspace import checkout_commit

    reported = checkout_commit(tmp_path)
    assert reported and sha.startswith(reported)


def test_a_directory_that_is_not_a_repository_reports_nothing(tmp_path):
    from quorum_review.workspace import checkout_commit

    assert checkout_commit(tmp_path) == ""
