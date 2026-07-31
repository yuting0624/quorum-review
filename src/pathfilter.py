"""Deciding which files are worth a model's attention.

Reviewing a lockfile, a build artefact, or a vendored dependency costs the same
as reviewing hand-written code and returns nothing. On a real repository this is
most of the diff, so filtering is not a nicety.

Two sources, both optional and additive:

- ``exclude`` on the action, for repository-specific paths.
- ``.quorumignore`` at the repository root, for the same thing kept in version
  control next to the code it describes.

Both use gitignore-style globs. Defaults cover what is nearly always noise.
"""

from __future__ import annotations

import fnmatch
import pathlib

IGNORE_FILE = ".quorumignore"

#: Paths that are generated, vendored, or otherwise not written by hand.
#: Deliberately conservative — anything a person might actually have edited
#: stays in scope, because a missed review is worse than a wasted one.
DEFAULT_EXCLUDES = (
    # Dependency manifests resolved by a tool
    "*.lock",
    "*.lockb",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Cargo.lock",
    "go.sum",
    "composer.lock",
    "Gemfile.lock",
    # Vendored and installed code
    "vendor/**",
    "node_modules/**",
    "third_party/**",
    "**/site-packages/**",
    # Build output
    "dist/**",
    "build/**",
    "out/**",
    "target/**",
    "*.min.js",
    "*.min.css",
    "*.map",
    # Generated sources
    "**/*_pb2.py",
    "**/*_pb2_grpc.py",
    "**/*.pb.go",
    "**/*.g.dart",
    "**/*.generated.*",
    "**/generated/**",
    # Test fixtures and recordings — large, and intentionally odd
    "**/__snapshots__/**",
    "**/cassettes/**",
    "**/testdata/**",
    "**/fixtures/**",
    # Binary and media
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.webp",
    "*.ico",
    "*.pdf",
    "*.zip",
    "*.gz",
    "*.woff",
    "*.woff2",
    "*.ttf",
)


def parse_patterns(raw: str) -> list[str]:
    """Read patterns from a newline- or comma-separated string.

    Blank lines and ``#`` comments are skipped, so the same parser handles an
    action input and a ``.quorumignore`` file.
    """
    patterns: list[str] = []
    for line in (raw or "").replace(",", "\n").splitlines():
        entry = line.strip()
        if entry and not entry.startswith("#"):
            patterns.append(entry)
    return patterns


def read_ignore_file(root: str | pathlib.Path = ".") -> list[str]:
    """Read ``.quorumignore`` from the checkout, if there is one."""
    path = pathlib.Path(root) / IGNORE_FILE
    if not path.is_file():
        return []
    return parse_patterns(path.read_text(encoding="utf-8"))


def matches(path: str, pattern: str) -> bool:
    """Whether a repository-relative path matches one gitignore-style glob.

    ``fnmatch`` gets most of the way on its own because its ``*`` crosses
    directory separators, so ``dist/**`` already matches ``dist/a/b.js``. Two
    cases it does not handle are added:

    - ``**/generated/**`` should match ``generated/x`` at the root, not only
      nested under something.
    - A bare name with no separator should match that file at any depth.
    """
    path = path.removeprefix("./")

    # A trailing slash, or a bare directory name, means everything beneath it.
    if pattern.endswith("/"):
        pattern = pattern[:-1] + "/**"

    if fnmatch.fnmatch(path, pattern):
        return True

    if pattern.startswith("**/"):
        tail = pattern.removeprefix("**/")
        segments = path.split("/")
        return any(
            fnmatch.fnmatch("/".join(segments[start:]), tail)
            for start in range(len(segments))
        )

    if "/" not in pattern:
        return fnmatch.fnmatch(pathlib.PurePosixPath(path).name, pattern)

    return False


class PathFilter:
    """Holds the active pattern set and answers "should this be reviewed?"."""

    def __init__(self, extra: list[str] | None = None, use_defaults: bool = True) -> None:
        self.patterns = list(DEFAULT_EXCLUDES) if use_defaults else []
        self.patterns += extra or []

    @classmethod
    def build(
        cls,
        exclude_input: str = "",
        ignore_file_contents: str = "",
        root: str | pathlib.Path | None = None,
        use_defaults: bool = True,
    ) -> PathFilter:
        """Combine the action input, a ``.quorumignore``, and the defaults.

        ``ignore_file_contents`` is passed in because in Actions the file is
        fetched from the API rather than read from disk — the action's working
        directory is its own checkout, not the repository under review.
        ``root`` is the local-development path.
        """
        patterns = parse_patterns(exclude_input) + parse_patterns(ignore_file_contents)
        if root is not None:
            patterns += read_ignore_file(root)
        return cls(patterns, use_defaults=use_defaults)

    def excluded(self, path: str) -> bool:
        return any(matches(path, pattern) for pattern in self.patterns)

    def partition(self, paths: list[str]) -> tuple[list[str], list[str]]:
        """Split into (to review, skipped)."""
        keep = [p for p in paths if not self.excluded(p)]
        skip = [p for p in paths if self.excluded(p)]
        return keep, skip
