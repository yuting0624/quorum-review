"""Shared input validation.

Anything that turns user input into a filesystem path goes through here first.
"""

import os
import re

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def safe_export_name(filename: str) -> str:
    """Reduce a user-supplied name to one safe path segment.

    Strips any directory part, rejects dotfiles and the traversal names, and
    requires the remainder to match a conservative allowlist. The result is
    always a single segment, so joining it to a directory cannot escape that
    directory.
    """
    name = os.path.basename(filename or "")
    if not name or name in (".", "..") or name.startswith("."):
        raise ValueError(f"unusable export name: {filename!r}")
    if not _SAFE_NAME.fullmatch(name):
        raise ValueError(f"unusable export name: {filename!r}")
    return name
