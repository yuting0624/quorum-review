"""Unified-diff helpers.

Kept deliberately small: enough to split a diff per file, drop binary blobs and
cap the size of any single file, and nothing more. A full diff parser is not
needed and would obscure what the reviewer actually does.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: Per-file cap on how much diff text is put into a prompt (PRD §9). Anything
#: beyond this is dropped with a marker so the model knows it is not seeing
#: the whole change.
DEFAULT_FILE_CHAR_LIMIT = 20_000

_HEADER = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$")


def split_by_file(diff: str) -> dict[str, str]:
    """Split a unified diff into ``{path: section}``.

    The path is taken from the ``b/`` side of the header, i.e. the name after
    the change, which matches the line numbers the model reports against.
    """
    sections: dict[str, str] = {}
    current_path: str | None = None
    buffer: list[str] = []

    for line in diff.splitlines(keepends=True):
        match = _HEADER.match(line.rstrip("\n"))
        if match:
            if current_path is not None:
                sections[current_path] = "".join(buffer)
            current_path = match.group("b")
            buffer = [line]
        elif current_path is not None:
            buffer.append(line)

    if current_path is not None:
        sections[current_path] = "".join(buffer)
    return sections


def is_binary(section: str) -> bool:
    return "GIT binary patch" in section or "Binary files " in section


def truncate(
    diff: str,
    file_char_limit: int = DEFAULT_FILE_CHAR_LIMIT,
) -> tuple[str, list[str]]:
    """Cap each file's section and drop binary patches.

    Returns the trimmed diff and the list of paths that were shortened or
    skipped, so the caller can say so in the summary comment rather than
    quietly reviewing a partial change.
    """
    sections = split_by_file(diff)
    if not sections:
        return diff, []

    kept: list[str] = []
    trimmed: list[str] = []

    for path, section in sections.items():
        if is_binary(section):
            trimmed.append(path)
            continue
        if len(section) > file_char_limit:
            section = (
                section[:file_char_limit]
                + f"\n... [truncated: {path} exceeds {file_char_limit} characters]\n"
            )
            trimmed.append(path)
        kept.append(section)

    return "".join(kept), trimmed


def select(diff: str, keep: Callable[[str], bool]) -> tuple[str, list[str]]:
    """Drop whole files from a diff, returning what remains and what went.

    Separate from :func:`truncate` because the two answer different questions —
    "is this file worth reviewing at all" and "is this file too big to send" —
    and the summary reports them differently.
    """
    sections = split_by_file(diff)
    if not sections:
        return diff, []

    kept = [section for path, section in sections.items() if keep(path)]
    dropped = [path for path in sections if not keep(path)]
    return "".join(kept), dropped


def for_file(diff: str, path: str) -> str:
    """Return just one file's section, for per-finding verification.

    Sending the whole diff on every verify call would multiply cost by the
    number of findings for no benefit — the verifier only needs the code it is
    being asked about.
    """
    return split_by_file(diff).get(path, "")
