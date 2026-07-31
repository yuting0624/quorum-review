"""Where the review criteria come from, and who is allowed to choose them.

Criteria are instructions to the model — a much more direct route than the diff,
which at least arrives wrapped in `<untrusted_*>` tags and labelled as data. So
"can this branch pick its own criteria" is a security question, not a
configuration one, and it has the same answer as `.quorumignore`.
"""

from __future__ import annotations

import asyncio

import pytest

from quorum_review import criteria


class FakeGitHub:
    def __init__(self, files: dict[tuple[str, str], str] | None = None) -> None:
        self.files = files or {}
        self.reads: list[tuple[str, str]] = []

    async def read_file(self, path: str, ref: str) -> str:
        self.reads.append((path, ref))
        return self.files.get((path, ref), "")


def resolve(spec: str, github: FakeGitHub | None = None, ref: str = "") -> object:
    return asyncio.run(criteria.resolve(spec, github, ref))


# -- built-ins -------------------------------------------------------------


def test_a_bare_name_is_a_builtin():
    skill = resolve("security-review")
    assert skill.name == "security-review"
    assert "Injection" in skill.content


def test_an_empty_spec_falls_back_to_the_default():
    """An unset action input arrives as an empty string, not as absent."""
    assert resolve("").name == "security-review"


def test_an_unknown_builtin_says_what_exists_and_how_to_use_your_own():
    with pytest.raises(FileNotFoundError) as error:
        resolve("our-standards")
    message = str(error.value)
    assert "security-review" in message
    assert ".github/quorum/backend.md" in message


def test_several_builtins_combine():
    skill = resolve("security-review, code-quality-review")
    assert "Injection" in skill.content
    assert len(skill.content) > len(resolve("security-review").content)


def test_newlines_separate_as_well_as_commas():
    """A YAML block scalar is the natural way to write more than one."""
    assert resolve("security-review\ncode-quality-review").name == (
        "security-review, code-quality-review"
    )


# -- criteria from the repository under review -----------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("security-review", False),
        ("code-quality-review", False),
        (".github/quorum/backend.md", True),
        ("docs/review.md", True),
        ("REVIEW.md", True),
    ],
)
def test_paths_are_told_apart_from_names(name: str, expected: bool):
    assert criteria.is_repository_path(name) is expected


def test_a_repository_path_is_read_at_the_given_ref():
    github = FakeGitHub({(".github/quorum/backend.md", "abc123"): "# Our standards"})
    skill = resolve(".github/quorum/backend.md", github, "abc123")

    assert "Our standards" in skill.content
    assert github.reads == [(".github/quorum/backend.md", "abc123")]


def test_the_ref_is_the_callers_choice_not_the_files():
    """review.py passes the base sha for a fork. This is the mechanism for it."""
    github = FakeGitHub({("r.md", "base-sha"): "base copy", ("r.md", "head-sha"): "head"})
    assert "base copy" in resolve("r.md", github, "base-sha").content


def test_a_builtin_and_a_repository_file_combine():
    github = FakeGitHub({("r.md", "sha"): "# Also check our thing"})
    skill = resolve("security-review, r.md", github, "sha")
    assert "Injection" in skill.content
    assert "Also check our thing" in skill.content


def test_a_missing_repository_file_explains_the_fork_case():
    """The likeliest cause of an empty read is exactly the security rule."""
    with pytest.raises(FileNotFoundError) as error:
        resolve("r.md", FakeGitHub(), "sha")
    assert "base branch" in str(error.value)


def test_a_repository_path_without_a_repository_is_refused():
    with pytest.raises(FileNotFoundError):
        resolve(".github/quorum/backend.md")


# -- bounds ----------------------------------------------------------------


def test_criteria_longer_than_the_limit_are_refused():
    """It is prepended to every scan prompt, so its length is a per-review cost."""
    github = FakeGitHub({("r.md", "sha"): "x" * (criteria.MAX_CRITERIA_CHARS + 1)})
    with pytest.raises(ValueError, match="characters"):
        resolve("r.md", github, "sha")


def test_criteria_at_the_limit_are_allowed():
    github = FakeGitHub({("r.md", "sha"): "x" * criteria.MAX_CRITERIA_CHARS})
    assert resolve("r.md", github, "sha").content


def test_too_many_files_are_refused():
    with pytest.raises(ValueError, match="limit is"):
        resolve(", ".join(["security-review"] * (criteria.MAX_CRITERIA_FILES + 1)))
