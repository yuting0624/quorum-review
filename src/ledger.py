"""The review ledger: stable identity for findings, and where their state lives.

The noise problem with automated review is not bad findings, it is the *same*
finding re-posted on every push. Line numbers move, so anything keyed on them
re-reports constantly. The ledger fixes identity to content instead, and stores
per-finding state so a later run can tell "already reported" from "new".

State is kept in a hidden HTML marker inside the summary comment. No external
storage, nothing for the adopter to configure, and the state travels with the
pull request it describes.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .schema import Finding

SCHEMA_VERSION = 1

MARKER_PREFIX = "<!-- quorum-state: "
MARKER_SUFFIX = " -->"
_MARKER = re.compile(
    re.escape(MARKER_PREFIX) + r"(?P<payload>[A-Za-z0-9+/=:]+)" + re.escape(MARKER_SUFFIX)
)

#: GitHub rejects issue comments over 65536 characters. Compress well before
#: that, then start dropping history if even the compressed form is too big.
COMMENT_CHAR_LIMIT = 65_536
COMPRESS_ABOVE = 4_096

_COMMENT = re.compile(r"(#|//).*$", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


def normalize_snippet(snippet: str) -> str:
    """Reduce a code snippet to something reformatting will not change.

    Strips comments and collapses all whitespace, so re-indentation, wrapping,
    or an added explanatory comment does not mint a new finding ID.

    This is not a parser: a ``#`` inside a string literal is treated as the
    start of a comment. That is acceptable because the only requirement is
    determinism — the same snippet must always hash the same way.
    """
    without_comments = _COMMENT.sub("", snippet)
    return _WHITESPACE.sub(" ", without_comments).strip()


def compute_finding_id(file_path: str, code_snippet: str) -> str:
    """Derive a stable ID from location and content.

    **The line number is deliberately excluded.** Adding an import above the
    finding, or moving a function, shifts every line below it; an ID that
    included the line would break and the finding would be re-reported as new.

    Known limitation: a renamed file yields a different ID, so the finding is
    treated as new. Left as-is for v1 and documented in the README.
    """
    digest = hashlib.sha256()
    digest.update(file_path.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(normalize_snippet(code_snippet).encode("utf-8"))
    return digest.hexdigest()[:16]


@dataclass
class LedgerEntry:
    """One tracked finding and everything known about it across runs."""

    finding_id: str
    file_path: str
    category: str
    severity: str
    title: str
    reported_by: list[str] = field(default_factory=list)
    verdict: str = ""
    verifier_model: str = ""
    verifier_reason: str = ""
    review_comment_id: int | None = None
    review_thread_id: str | None = None
    status: str = "open"  # open | fixed | wontfix
    first_seen_sha: str = ""
    resolved_sha: str | None = None
    wontfix_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in self.__dict__.items()}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LedgerEntry:
        known = {key: raw.get(key) for key in cls.__dataclass_fields__ if key in raw}
        return cls(**known)  # type: ignore[arg-type]

    @classmethod
    def from_finding(cls, finding: Finding, head_sha: str) -> LedgerEntry:
        return cls(
            finding_id=finding.finding_id,
            file_path=finding.file_path,
            category=finding.category,
            severity=finding.severity,
            title=finding.title,
            reported_by=list(finding.reported_by),
            verdict=finding.verdict,
            verifier_model=finding.verifier_model,
            verifier_reason=finding.verifier_reason,
            first_seen_sha=head_sha,
        )


@dataclass
class Ledger:
    """The full state document embedded in the summary comment."""

    pr_number: int
    last_reviewed_sha: str = ""
    interaction_id: str = ""
    environment_id: str = ""
    entries: dict[str, LedgerEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    # -- lookup ------------------------------------------------------------

    def known(self, finding_id: str) -> LedgerEntry | None:
        return self.entries.get(finding_id)

    def is_suppressed(self, finding_id: str) -> bool:
        """True when this finding must not be posted again.

        Covers both "already reported and still open" and "the author declared
        it a false positive". Either way, re-posting is noise.
        """
        entry = self.entries.get(finding_id)
        return entry is not None and entry.status in ("open", "wontfix")

    def record(self, entry: LedgerEntry) -> None:
        existing = self.entries.get(entry.finding_id)
        if existing is None:
            self.entries[entry.finding_id] = entry
            return
        # Keep the original sighting and any human decision already made.
        entry.first_seen_sha = existing.first_seen_sha or entry.first_seen_sha
        if existing.status == "wontfix":
            entry.status = "wontfix"
            entry.wontfix_reason = existing.wontfix_reason
        entry.review_comment_id = entry.review_comment_id or existing.review_comment_id
        entry.review_thread_id = entry.review_thread_id or existing.review_thread_id
        self.entries[entry.finding_id] = entry

    def mark_fixed(self, finding_id: str, head_sha: str) -> None:
        entry = self.entries.get(finding_id)
        if entry is not None and entry.status == "open":
            entry.status = "fixed"
            entry.resolved_sha = head_sha

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pr_number": self.pr_number,
            "last_reviewed_sha": self.last_reviewed_sha,
            "interaction_id": self.interaction_id,
            "environment_id": self.environment_id,
            "findings": [entry.to_dict() for entry in self.entries.values()],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Ledger:
        ledger = cls(
            pr_number=int(raw.get("pr_number", 0)),
            last_reviewed_sha=str(raw.get("last_reviewed_sha", "")),
            interaction_id=str(raw.get("interaction_id", "")),
            environment_id=str(raw.get("environment_id", "")),
            schema_version=int(raw.get("schema_version", SCHEMA_VERSION)),
        )
        for item in raw.get("findings", []) or []:
            if not isinstance(item, dict) or not item.get("finding_id"):
                continue
            entry = LedgerEntry.from_dict(item)
            ledger.entries[entry.finding_id] = entry
        return ledger

    @classmethod
    def empty(cls, pr_number: int) -> Ledger:
        return cls(pr_number=pr_number)


# --------------------------------------------------------------------------
# Marker encoding
# --------------------------------------------------------------------------
#
# The payload is base64 with a one-character tag so the encoding is
# self-describing: "j:" is plain JSON, "z:" is gzipped JSON. Small ledgers stay
# readable to anyone who base64-decodes the marker out of curiosity, and large
# ones still fit.


def encode_marker(ledger: Ledger) -> str:
    payload = json.dumps(ledger.to_dict(), separators=(",", ":"), ensure_ascii=False)
    raw = payload.encode("utf-8")

    if len(raw) > COMPRESS_ABOVE:
        body = "z:" + base64.b64encode(gzip.compress(raw)).decode("ascii")
    else:
        body = "j:" + base64.b64encode(raw).decode("ascii")

    return f"{MARKER_PREFIX}{body}{MARKER_SUFFIX}"


def decode_marker(comment_body: str) -> Ledger | None:
    """Recover the ledger from a comment, or None if there is not one.

    A corrupt marker is treated as absent rather than fatal. Losing history
    degrades the review to a cold start; refusing to run helps no one.
    """
    match = _MARKER.search(comment_body or "")
    if not match:
        return None

    body = match.group("payload")
    try:
        tag, _, encoded = body.partition(":")
        raw = base64.b64decode(encoded)
        if tag == "z":
            raw = gzip.decompress(raw)
        return Ledger.from_dict(json.loads(raw.decode("utf-8")))
    except Exception:  # noqa: BLE001 - a bad marker must not stop the review
        return None


def fit_to_comment(ledger: Ledger, visible_body: str) -> str:
    """Serialise the ledger so the finished comment stays under GitHub's limit.

    When it does not fit, resolved findings are dropped oldest-first: they carry
    the least value, since a fixed issue re-detected later is a genuine
    regression worth reporting again.
    """
    marker = encode_marker(ledger)
    if len(visible_body) + len(marker) <= COMMENT_CHAR_LIMIT:
        return marker

    fixed_ids = [
        entry.finding_id for entry in ledger.entries.values() if entry.status == "fixed"
    ]
    for finding_id in fixed_ids:
        ledger.entries.pop(finding_id, None)
        marker = encode_marker(ledger)
        if len(visible_body) + len(marker) <= COMMENT_CHAR_LIMIT:
            return marker

    # Still too large: keep the pointer to HEAD and give up on the history.
    minimal = Ledger(
        pr_number=ledger.pr_number, last_reviewed_sha=ledger.last_reviewed_sha
    )
    return encode_marker(minimal)


def assign_ids(findings: list[Finding]) -> list[Finding]:
    """Populate ``finding_id`` on each finding, in place."""
    for finding in findings:
        finding.finding_id = compute_finding_id(finding.file_path, finding.code_snippet)
    return findings
