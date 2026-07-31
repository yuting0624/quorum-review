"""Read-only access to the rest of the repository, offered to the models as tools.

A diff is not enough to review a diff. Whether `has_scope(user, "document.report")`
is a real check depends on a registry the pull request never touches; whether
`safe_export_name(filename)` makes a path join safe depends on a validator
defined elsewhere. A reviewer that can only see added lines has to guess at both,
and guessing produces exactly the two failures that make a review bot unusable:
confident false positives on code that is guarded upstream, and silence on bugs
whose evidence lives one file away.

So the models get to read the checkout. Three tools, all read-only:

- ``read_file``   — a slice of one file, with line numbers
- ``search``      — a regular expression across the repository
- ``list_files``  — what is in a directory

The bounds matter as much as the tools:

**Nothing here writes or executes.** The content being reviewed is attacker
controlled — anyone who can open a pull request can put text in the diff. A
prompt injection that fully succeeds against this toolbox gains the ability to
read files that the same workflow already checked out, and nothing more.

**Confined to the checkout.** Paths are resolved before use and rejected if they
land outside the root, so ``../../.ssh/id_rsa`` and a symlink pointing at it are
both refused.

**Filtered like the review itself.** The same exclusion set that keeps lockfiles
and vendored code out of the diff keeps them out of the toolbox, so an
exploration loop cannot spend its budget reading ``node_modules``.

**Budgeted.** Calls, bytes returned, and conversation turns are all capped. An
agent loop with no ceiling is a bill with no ceiling, and a review that takes
twenty minutes to arrive is a review nobody waits for.
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any

from .pathfilter import PathFilter

#: How many tool calls one model may make in one scan. Reached in practice only
#: by a model that has lost the thread; the C1-C3 cases resolve in three or four.
MAX_CALLS = 24

#: Total bytes of tool output returned to one model. Past this, further calls
#: are refused with an explanation rather than silently truncated, so the model
#: knows it is working from what it already has.
MAX_TOTAL_BYTES = 400_000

#: Per-call ceilings.
MAX_FILE_BYTES = 60_000
MAX_LINES = 400
MAX_MATCHES = 60
MAX_ENTRIES = 200

#: Files walked by one ``search`` call. A large monorepo would otherwise spend
#: the whole review budget on one regular expression.
MAX_FILES_WALKED = 6_000

#: Assistant turns in the exploration loop. Each turn may contain several calls,
#: so this is a bound on round trips rather than on tool use.
MAX_TURNS = 8

#: Never readable, regardless of the repository's own configuration.
ALWAYS_DENY = ("**/.git/**", ".git/**", "**/.env", "**/.env.*", "**/*.pem", "**/*.key")

#: One neutral declaration set, rendered into each vendor's shape at the call
#: site. Keeping it in one place is what stops the two models from being offered
#: subtly different tools, which would make their disagreement uninterpretable.
TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read a slice of a file from the repository, with line numbers. "
            "Use this to check whether a function called in the diff actually "
            "does what its name suggests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Repository-relative path, e.g. app/permissions.py",
                },
                "start_line": {
                    "type": "integer",
                    "description": "First line to return, 1-based. Defaults to 1.",
                },
                "line_count": {
                    "type": "integer",
                    "description": f"How many lines to return, up to {MAX_LINES}.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "search",
        "description": (
            "Search the repository with a Python regular expression and return "
            "matching lines with their locations. Use this to find where "
            "something is defined, or every place it is called."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Python regular expression.",
                },
                "path_glob": {
                    "type": "string",
                    "description": (
                        "Optional glob limiting which files are searched, "
                        "e.g. '*.py' or 'app/**'."
                    ),
                },
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "list_files",
        "description": "List the files and directories under a repository path.",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "Repository-relative directory. Defaults to root.",
                }
            },
            "required": [],
        },
    },
]


def workspace_root() -> pathlib.Path | None:
    """Where the repository under review is checked out, if it is.

    In Actions this is ``GITHUB_WORKSPACE``. It is deliberately not the current
    working directory: a composite action runs from its own checkout, so
    ``Path.cwd()`` is this project rather than the repository being reviewed —
    a mistake already made once, in the code that reads ``.quorumignore``.

    ``QUORUM_WORKSPACE`` overrides it, which a fork review needs: there the
    workspace holds the *base* repository and the fork's code is checked out
    into a subdirectory, because running a fork's build is the thing that
    turns ``pull_request_target`` into a compromise.
    """
    override = os.getenv("QUORUM_WORKSPACE", "").strip()
    if override:
        candidate = pathlib.Path(override)
        return candidate.resolve() if candidate.is_dir() else None

    raw = os.getenv("GITHUB_WORKSPACE", "").strip()
    if not raw:
        return None
    root = pathlib.Path(raw)
    return root.resolve() if root.is_dir() else None


def access_enabled() -> bool:
    """Whether the models may read files outside the diff.

    On by default, because off is the setting that produces the two failures
    people actually complain about — a false positive on code that is guarded
    somewhere the reviewer could not look, and silence on a bug whose evidence
    is one file away. It costs extra turns, so ``QUORUM_REPO_ACCESS=off``
    restores the diff-only behaviour.
    """
    return os.getenv("QUORUM_REPO_ACCESS", "on").strip().lower() not in {
        "off",
        "0",
        "false",
        "no",
    }


def build(
    count: int,
    max_calls: int = MAX_CALLS,
    patterns: list[str] | None = None,
) -> list[Workspace | None]:
    """One independent budget per caller, or ``None`` when there is no checkout.

    Budgets are not shared. Two models exploring the same repository must not be
    able to starve each other, for the same reason their scans do not see each
    other: the moment one model's behaviour changes what the other is allowed to
    do, their agreement stops being evidence.

    ``patterns`` is the exclusion set already resolved for the diff. Passing it
    in is the point: the checkout *is* the branch under review, so reading
    ``.quorumignore`` from it here would let a fork's copy govern the tools
    while the base's copy governs the diff. Two reads of one policy file at two
    different refs is the shape of bug nobody finds by reading either half.
    The fallback below only applies when nothing was resolved — a local run.
    """
    root = workspace_root()
    if root is None or not access_enabled():
        return [None] * count

    if patterns is None:
        path_filter = PathFilter.build(
            exclude_input=os.getenv("QUORUM_EXCLUDE", ""), root=root
        )
    else:
        path_filter = PathFilter(list(patterns), use_defaults=False)

    return [Workspace(root, path_filter, max_calls=max_calls) for _ in range(count)]


def checkout_commit(root: pathlib.Path) -> str:
    """What the checkout is actually at, short form, or "".

    Reported alongside the commit under review because the two are not the
    same object and can disagree. ``refs/pull/N/merge`` is recomputed by GitHub
    whenever the base branch moves, and it is resolved when the workflow checks
    out — not when the diff was fetched. So the tree the models read can be a
    merge against a newer base than the diff describes.

    The window is small and the newer base is usually the more correct one, so
    this is surfaced rather than prevented: a reader chasing a finding that
    does not match what they see locally deserves to know which tree it came
    from. Preventing it would mean checking out the head alone, which costs the
    base context that repository access exists to provide.
    """
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def checkout_has_commit(root: pathlib.Path, sha: str) -> bool:
    """Whether the checkout actually contains the commit under review.

    A workflow triggered by ``issue_comment`` checks out the default branch, not
    the pull request, so the tools would read whatever is on main while the diff
    describes the branch. Usually the two agree on the untouched files the
    reviewer wants to consult — but "usually" is not a property to build on
    silently, so the mismatch is detected and reported.

    Presence of the object is the test rather than ``HEAD``: a pull-request
    checkout resolves to a merge commit whose SHA is nothing the API returned,
    and the head commit is its parent.
    """
    if not sha:
        return False
    import subprocess

    try:
        completed = subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=root,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class Workspace:
    """A budgeted, read-only view of the checkout, shared by all models in a run.

    The budget is per-instance and each model gets its own, so one model
    exhausting its allowance does not silence the other. That independence is
    the same reason the two scans do not see each other's findings.
    """

    def __init__(
        self,
        root: pathlib.Path,
        path_filter: PathFilter | None = None,
        max_calls: int = MAX_CALLS,
    ) -> None:
        self.root = root.resolve()
        self._filter = path_filter or PathFilter()
        self._filter.patterns = list(self._filter.patterns) + list(ALWAYS_DENY)

        self.max_calls = max_calls
        self.calls = 0
        self.bytes_returned = 0
        #: Files actually opened, in order. Surfaced in the summary comment so a
        #: reader can see what the reviewer looked at beyond the diff.
        self.files_read: list[str] = []

    # -- budget ------------------------------------------------------------

    @property
    def exhausted(self) -> bool:
        return self.calls >= self.max_calls or self.bytes_returned >= MAX_TOTAL_BYTES

    def summary(self) -> str:
        """One line describing what was read, for the run summary."""
        if not self.calls and not self.files_read:
            return "no files read beyond the diff"
        unique = list(dict.fromkeys(self.files_read))
        shown = ", ".join(f"`{name}`" for name in unique[:6])
        if len(unique) > 6:
            shown += f", and {len(unique) - 6} more"
        if not unique:
            return f"{self.calls} repository searches, no files opened"
        return f"{self.calls} tool calls; read {shown}"

    # -- dispatch ----------------------------------------------------------

    def run(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute one tool call and return its output as text.

        Never raises. A tool that fails returns a sentence saying why, because
        the model can act on that — retry with a different path, or conclude
        the file does not exist — whereas an exception ends the whole review
        over something incidental.
        """
        if self.exhausted:
            return (
                "Tool budget exhausted. No further calls will be answered; "
                "produce your findings from what you have."
            )

        self.calls += 1
        try:
            result = self._dispatch(name, arguments or {})
        except Exception as error:  # noqa: BLE001 - reported to the model as text
            return f"Error: {type(error).__name__}: {error}"

        self.bytes_returned += len(result.encode("utf-8", errors="ignore"))
        return result

    def _dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "read_file":
            return self.read_file(
                str(arguments.get("path", "")),
                _as_int(arguments.get("start_line"), 1),
                _as_int(arguments.get("line_count"), MAX_LINES),
            )
        if name == "search":
            return self.search(
                str(arguments.get("pattern", "")),
                str(arguments.get("path_glob") or ""),
            )
        if name == "list_files":
            return self.list_files(str(arguments.get("directory") or ""))
        return f"Error: unknown tool {name!r}"

    # -- tools -------------------------------------------------------------

    def read_file(
        self, path: str, start_line: int = 1, line_count: int = MAX_LINES
    ) -> str:
        target, relative = self._resolve(path)
        if not target.is_file():
            return f"Error: {relative} is not a file in this repository."

        data = target.read_bytes()[:MAX_FILE_BYTES]
        if b"\0" in data[:4096]:
            return f"Error: {relative} looks like a binary file."

        lines = data.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        count = max(1, min(line_count, MAX_LINES))
        window = lines[start - 1 : start - 1 + count]
        if not window:
            return f"{relative} has {len(lines)} lines; line {start} is past the end."

        self.files_read.append(relative)
        numbered = "\n".join(
            f"{number:>5}  {text}" for number, text in enumerate(window, start=start)
        )
        end = start + len(window) - 1
        note = "" if end >= len(lines) else f"\n... ({len(lines) - end} more lines)"
        return f"{relative} lines {start}-{end} of {len(lines)}:\n{numbered}{note}"

    def search(self, pattern: str, path_glob: str = "") -> str:
        if not pattern:
            return "Error: search needs a pattern."
        try:
            expression = re.compile(pattern)
        except re.error as error:
            return f"Error: {pattern!r} is not a valid regular expression: {error}"

        matches: list[str] = []
        walked = 0
        for path in self._walk():
            walked += 1
            if walked > MAX_FILES_WALKED:
                matches.append(f"... (stopped after {MAX_FILES_WALKED} files)")
                break
            relative = path.relative_to(self.root).as_posix()
            if path_glob and not _glob_matches(relative, path_glob):
                continue
            try:
                data = path.read_bytes()[:MAX_FILE_BYTES]
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            for number, line in enumerate(
                data.decode("utf-8", errors="replace").splitlines(), start=1
            ):
                if expression.search(line):
                    matches.append(f"{relative}:{number}: {line.strip()[:200]}")
                    if len(matches) >= MAX_MATCHES:
                        matches.append(f"... (stopped at {MAX_MATCHES} matches)")
                        return "\n".join(matches)

        return "\n".join(matches) if matches else f"No matches for {pattern!r}."

    def list_files(self, directory: str = "") -> str:
        target, relative = self._resolve(directory or ".")
        if not target.is_dir():
            return f"Error: {relative} is not a directory in this repository."

        entries: list[str] = []
        for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
            child_relative = child.relative_to(self.root).as_posix()
            if self._excluded(child_relative, child.is_dir()):
                continue
            entries.append(child_relative + ("/" if child.is_dir() else ""))
            if len(entries) >= MAX_ENTRIES:
                entries.append(f"... (stopped at {MAX_ENTRIES} entries)")
                break
        return "\n".join(entries) if entries else f"{relative} is empty."

    # -- internals ---------------------------------------------------------

    def _resolve(self, path: str) -> tuple[pathlib.Path, str]:
        """Map a model-supplied path to a real one, or refuse.

        Resolution happens before the containment check so that a symlink
        pointing out of the checkout is caught, not just a literal ``..``.
        """
        cleaned = (path or "").strip().replace("\\", "/").lstrip("/")
        candidate = (self.root / cleaned).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise ValueError(f"{path!r} is outside the repository")

        if candidate == self.root:
            relative = "."
        else:
            relative = candidate.relative_to(self.root).as_posix()
        if relative != "." and self._excluded(relative, candidate.is_dir()):
            raise ValueError(f"{relative} is excluded from review")
        return candidate, relative

    def _excluded(self, relative: str, is_dir: bool = False) -> bool:
        """Whether a path is off limits, treating a fully-excluded directory as one.

        ``node_modules/**`` hides everything under the directory but not the
        directory's own name, so a plain check would list a path where every
        subsequent read fails. Probing one level down closes that gap and, in
        ``_walk``, stops the search descending into a tree it must then discard
        file by file.
        """
        if self._filter.excluded(relative):
            return True
        return is_dir and self._filter.excluded(f"{relative}/probe")

    def _walk(self):
        """Every non-excluded file under the root, depth first."""
        stack = [self.root]
        while stack:
            current = stack.pop()
            try:
                children = list(current.iterdir())
            except OSError:
                continue
            for child in children:
                relative = child.relative_to(self.root).as_posix()
                if child.is_symlink():
                    continue
                if self._excluded(relative, child.is_dir()):
                    continue
                if child.is_dir():
                    stack.append(child)
                elif child.is_file():
                    yield child


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _glob_matches(path: str, pattern: str) -> bool:
    from .pathfilter import matches

    return matches(path, pattern)
