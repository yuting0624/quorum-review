"""Text extraction for the search index.

Extraction is delegated to `pdftotext` rather than reimplemented.
"""

import os
import subprocess

from .config import Config

EXTRACT_TIMEOUT_SECONDS = 30


def extract_text(user: dict, filename: str) -> str:
    """Extract plain text from a PDF the user already exported.

    ``filename`` is resolved against the caller's own export directory and
    checked to stay inside it before it is used, so it cannot escape into the
    rest of the filesystem.
    """
    user_dir = os.path.realpath(os.path.join(Config.EXPORT_ROOT, str(user["id"])))
    candidate = os.path.realpath(os.path.join(user_dir, filename))
    if os.path.commonpath([user_dir, candidate]) != user_dir:
        raise ValueError("path escapes the export directory")
    if not os.path.isfile(candidate):
        raise FileNotFoundError(filename)

    # shell=False with a literal argv: the path is passed as its own argument
    # and is never interpreted by a shell.
    result = subprocess.run(
        ["/usr/bin/pdftotext", "-q", candidate, "-"],
        capture_output=True,
        text=True,
        timeout=EXTRACT_TIMEOUT_SECONDS,
        check=False,
        shell=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pdftotext failed: {result.stderr.strip()}")
    return result.stdout
