"""The package version and the tag the documentation points at must agree.

`v1` once sat 38 commits behind `main` while every example said `@v1`, so the
documented install path shipped without the feature the README measures and
without two security fixes. The alias is moved by a workflow now — but the
workflow derives the alias from the tag, so a release numbered `0.2.0` moves
`v0` and leaves `v1` exactly where it was. Which is what happened.

Nothing in a workflow can catch that, because the mistake is made before the
tag exists. This can.
"""

from __future__ import annotations

import pathlib
import re

import quorum_review

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTION_REF = re.compile(r"quorum-review@v(\d+)")


def documents() -> list[pathlib.Path]:
    return [
        *ROOT.glob("*.md"),
        *ROOT.glob("docs/*.md"),
        *ROOT.glob("examples/*.yml"),
        *ROOT.glob(".github/workflows/*.yml"),
    ]


def test_the_version_is_a_three_part_number():
    assert re.fullmatch(r"\d+\.\d+\.\d+", quorum_review.__version__)


def test_pyproject_agrees_with_the_package():
    """The wheel's metadata and the value stamped on every finding are two
    different strings that have to say the same thing."""
    declared = re.search(
        r'^version = "([^"]+)"',
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert declared and declared.group(1) == quorum_review.__version__


def test_every_documented_action_reference_matches_the_major_version():
    """A release whose major does not match the documented alias moves a tag
    nobody uses and leaves the one everybody uses pointing at old code."""
    major = quorum_review.__version__.split(".")[0]

    referenced: dict[str, list[str]] = {}
    for path in documents():
        for found in ACTION_REF.findall(path.read_text(encoding="utf-8")):
            referenced.setdefault(found, []).append(str(path.relative_to(ROOT)))

    assert referenced, "no document tells anyone which version to use"
    wrong = {ref: where for ref, where in referenced.items() if ref != major}
    assert not wrong, (
        f"__version__ is {quorum_review.__version__}, so the alias is v{major}, "
        f"but these point elsewhere: {wrong}"
    )
