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

from .matching import Report, normalize_snippet, same_defect
from .schema import Finding

__all__ = [
    "Ledger",
    "LedgerEntry",
    "assign_ids",
    "compute_finding_id",
    "decode_marker",
    "encode_marker",
    "fit_to_comment",
    "normalize_snippet",
]

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

def _report_of(finding: Finding) -> Report:
    return Report(
        finding.file_path, finding.line, finding.code_snippet, finding.title
    )


def compute_finding_id(file_path: str, code_snippet: str) -> str:
    """Derive a stable ID from location and content.

    **The line number is deliberately excluded.** Adding an import above the
    finding, or moving a function, shifts every line below it; an ID that
    included the line would break and the finding would be re-reported as new.

    The ID alone is **not** sufficient to recognise a returning finding: it
    hashes the code the model chose to quote, and models do not quote the same
    bug identically twice. ``Ledger.find_match`` falls back to positional
    matching for that reason — see ``matching.same_defect``.

    Known limitation: a renamed file yields a different ID and does not match
    positionally either, so the finding is treated as new. Documented in the
    README.
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
    #: Where it was last seen, and the code that was quoted. Stored so a later
    #: run can recognise the same defect even when the model quotes it
    #: differently and the ID therefore changes.
    line: int = 0
    snippet: str = ""
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

    def as_report(self) -> Report:
        return Report(self.file_path, self.line, self.snippet, self.title)

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
            line=finding.line,
            snippet=normalize_snippet(finding.code_snippet),
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

    def find_match(self, finding: Finding) -> LedgerEntry | None:
        """Find the entry describing this defect, if it is already tracked.

        The ID is tried first because it is exact and cheap. It is not enough on
        its own: it hashes the code the model quoted, and a second run of the
        same model on the same diff routinely quotes a different span, yielding
        a different ID for the same bug. Positional matching catches those.
        """
        exact = self.entries.get(finding.finding_id)
        if exact is not None:
            return exact

        report = _report_of(finding)
        return next(
            (
                entry
                for entry in self.entries.values()
                if same_defect(report, entry.as_report())
            ),
            None,
        )

    def is_suppressed(self, finding: Finding) -> bool:
        """True when this finding must not be posted again.

        Covers both "already reported and still open" and "the author declared
        it a false positive". Either way, re-posting is noise.
        """
        entry = self.find_match(finding)
        return entry is not None and entry.status in ("open", "wontfix")

    def still_present(self, entry: LedgerEntry, findings: list[Finding]) -> bool:
        """Whether this run re-found a tracked defect."""
        report = entry.as_report()
        return any(
            entry.finding_id == finding.finding_id
            or same_defect(report, _report_of(finding))
            for finding in findings
        )

    def record(self, entry: LedgerEntry) -> None:
        """Store an entry, folding it into an existing one for the same defect.

        The lookup is positional as well as by ID: without that, a bug whose
        quoted snippet shifted between runs would accumulate one ledger entry
        per review, and the history that suppression depends on would be spread
        across all of them.
        """
        existing = self.entries.get(entry.finding_id)
        if existing is None:
            report = entry.as_report()
            existing = next(
                (
                    candidate
                    for candidate in self.entries.values()
                    if same_defect(report, candidate.as_report())
                ),
                None,
            )

        if existing is None:
            self.entries[entry.finding_id] = entry
            return

        # Keep the identity we already published, and any human decision made
        # against it — a comment already exists under the original ID.
        entry.finding_id = existing.finding_id
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


def strip_marker(comment_body: str) -> str:
    """Return the human-visible part of a summary comment."""
    return _MARKER.sub("", comment_body or "").rstrip()


def replace_marker(comment_body: str, ledger: Ledger) -> str:
    """Swap in an updated ledger while leaving the prose untouched.

    Used by flows that change state without re-reviewing — dismissing a false
    positive, for one. Re-rendering the whole summary there would mean
    inventing a report for a review that never ran.
    """
    visible = strip_marker(comment_body)
    return f"{visible}\n\n{fit_to_comment(ledger, visible)}"


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
