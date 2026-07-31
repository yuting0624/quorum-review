"""Resolving which review criteria to use, including the adopter's own.

The built-in criteria are a starting point, not a standard. A team that adopts
this has a checklist already — the thing their security review actually asks
about, the mistake their last incident came from, the framework convention that
keeps getting broken. That checklist lives in *their* repository, and until now
using it meant forking this one, which is not a thing anyone does for a review
bot.

So ``skill`` accepts three shapes, and they compose:

    skill: security-review                        a built-in
    skill: .github/quorum/backend.md              a file in the repository
    skill: security-review, .github/quorum/backend.md

Anything containing ``/`` or ending in ``.md`` is a repository path. Everything
else names a directory under this action's ``skills/``.

**Which ref repository criteria are read from is a security decision.** Criteria
are instructions to the model — a far more direct route than the diff, which at
least arrives wrapped in ``<untrusted_*>`` tags and labelled as data. For a
same-repository pull request the author already has write access, so editing the
criteria is no more than editing the workflow. For a fork it is not: the base
branch's copy governs, the same rule and the same reason as ``.quorumignore``.
"""

from __future__ import annotations

import pathlib
from typing import Any

from .schema import Skill

BUILTIN_ROOT = pathlib.Path(__file__).resolve().parent.parent / "skills"

#: Cap on one repository-supplied criteria file. It is prepended to every scan
#: prompt for every model, so its cost is paid on each review forever. Long
#: criteria also read worse: a model given four thousand words of policy starts
#: reporting policy violations rather than defects.
MAX_CRITERIA_CHARS = 20_000

#: Cap on how many can be combined, for the same reason.
MAX_CRITERIA_FILES = 4


def parse(spec: str) -> list[str]:
    """Split a ``skill`` input into individual names or paths."""
    return [
        part.strip()
        for part in (spec or "").replace(",", "\n").splitlines()
        if part.strip()
    ]


def is_repository_path(name: str) -> bool:
    return "/" in name or name.endswith(".md")


def builtin_names() -> list[str]:
    if not BUILTIN_ROOT.is_dir():
        return []
    return sorted(p.name for p in BUILTIN_ROOT.iterdir() if p.is_dir())


def load_builtin(name: str) -> Skill:
    path = BUILTIN_ROOT / name / "SKILL.md"
    if not path.exists():
        available = ", ".join(builtin_names())
        raise FileNotFoundError(
            f"unknown skill {name!r}; built-ins are: {available}. "
            f"For criteria in your own repository, give a path — "
            f"for example .github/quorum/backend.md"
        )
    return Skill(name=name, content=path.read_text(encoding="utf-8"))


async def resolve(spec: str, github: Any = None, ref: str = "") -> Skill:
    """Build one Skill from a possibly-composite ``skill`` input.

    ``github`` and ``ref`` are only needed when the spec names a repository
    path. Passing neither restricts the input to built-ins, which is what the
    dry-run and benchmark paths want.
    """
    names = parse(spec) or ["security-review"]
    if len(names) > MAX_CRITERIA_FILES:
        raise ValueError(
            f"{len(names)} criteria files requested; the limit is "
            f"{MAX_CRITERIA_FILES}. Every one of them is prepended to every "
            f"scan prompt, and a model given pages of policy starts reporting "
            f"policy violations rather than defects."
        )

    sections: list[str] = []
    for name in names:
        if not is_repository_path(name):
            sections.append(load_builtin(name).content)
            continue

        if github is None:
            raise FileNotFoundError(
                f"{name!r} looks like a repository path, but no repository is "
                f"available to read it from"
            )
        sections.append(_from_repository(await github.read_file(name, ref), name))

    return Skill(name=", ".join(names), content="\n\n---\n\n".join(sections))


def _from_repository(content: str, name: str) -> str:
    if not content.strip():
        raise FileNotFoundError(
            f"{name} is empty or does not exist at the ref being reviewed. "
            f"Repository criteria are read from the base branch for a fork's "
            f"pull request, so a file added by that pull request is not yet "
            f"visible to it."
        )
    if len(content) > MAX_CRITERIA_CHARS:
        raise ValueError(
            f"{name} is {len(content)} characters; the limit is "
            f"{MAX_CRITERIA_CHARS}. It is sent with every scan, so its length "
            f"is a per-review cost."
        )
    return content
