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

#: Cap on the whole diff. The per-file limit alone bounds nothing useful: a
#: dependency bump or a generated-code refactor can touch four hundred files
#: that each pass it comfortably, and the prompt built from that is both
#: expensive and worse — recall drops long before a context window fills.
#:
#: Files are kept smallest-first once this binds, which is the opposite of what
#: you might reach for. A five-hundred-line reformat is unlikely to be the
#: interesting change in a diff that also touches six small files, and dropping
#: six reviewable files to fit one unreviewable one is a bad trade.
DEFAULT_TOTAL_CHAR_LIMIT = 400_000

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
    total_char_limit: int = DEFAULT_TOTAL_CHAR_LIMIT,
) -> tuple[str, list[str], list[str]]:
    """Cap each file, drop binary patches, and keep the whole thing bounded.

    Returns ``(diff, trimmed, dropped)``. ``trimmed`` is files that were
    shortened or are binary; ``dropped`` is files that did not fit the total
    budget at all. They are reported separately because they mean different
    things to a reader: a trimmed file was partly reviewed, a dropped one was
    not looked at.
    """
    sections = split_by_file(diff)
    if not sections:
        return diff, [], []

    trimmed: list[str] = []
    capped: dict[str, str] = {}

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
        capped[path] = section

    # Smallest first, so a budget that binds costs the fewest files. Ties go to
    # the original diff order, which is alphabetical by path in git's output —
    # arbitrary, but stable, so the same pull request drops the same files on
    # every run rather than shuffling what gets reviewed.
    order = sorted(capped, key=lambda path: (len(capped[path]), path))

    kept: list[str] = []
    dropped: list[str] = []
    spent = 0
    for path in order:
        section = capped[path]
        if spent + len(section) > total_char_limit and kept:
            dropped.append(path)
            continue
        kept.append(path)
        spent += len(section)

    # Emit in the diff's own order, not the order the budget considered them.
    body = "".join(capped[path] for path in sections if path in set(kept))
    return body, trimmed, sorted(dropped)


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
