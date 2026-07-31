"""The GitHub Actions surface: outputs, the job summary, and the exit code.

A reviewer that only posts comments is a thing people read when they remember
to. To be part of a workflow it has to be something the workflow can act on —
a required check that blocks a merge, an output another step can branch on, a
summary that appears in the run without opening the pull request.

All three are one-way writes into the runner's environment, kept here rather
than scattered through the orchestrator so that running outside Actions is the
uninteresting case it should be: every function below is a no-op when the
corresponding environment variable is absent.
"""

from __future__ import annotations

import os
import pathlib
import sys

from .report import RunReport
from .schema import SEVERITIES, SEVERITY_RANK

#: Severities that can gate a merge, worst first, plus the default of not
#: gating at all. Deliberately not a boolean: "block on anything at all" and
#: "block on exploitable-now" are different policies and teams want both.
FAIL_ON_CHOICES = ("never", *SEVERITIES)


def fail_on() -> str:
    value = os.getenv("QUORUM_FAIL_ON", "never").strip().lower()
    return value if value in FAIL_ON_CHOICES else "never"


def fail_on_degraded() -> bool:
    """Whether a review that could not run properly should fail the check.

    Off by default, and the default is the interesting decision. A required
    check that passes because the reviewer was broken is worse than no check —
    but a check that blocks every merge in the organisation because one Vertex
    region is having a bad afternoon is its own outage. So the two policies are
    separate switches, and this one is opt-in.
    """
    return os.getenv("QUORUM_FAIL_ON_DEGRADED", "").strip().lower() in {
        "1",
        "on",
        "true",
        "yes",
    }


def posted(report: RunReport) -> list:
    """Findings the reader is actually being shown as real.

    Advisory and refuted findings are excluded on purpose: gating on a finding
    the reviewer itself declined to stand behind would teach people that the
    verdicts mean nothing.
    """
    return list(report.confirmed) + list(report.unverified)


def severity_counts(report: RunReport) -> dict[str, int]:
    counts = dict.fromkeys(SEVERITIES, 0)
    for finding in posted(report):
        if finding.severity in counts:
            counts[finding.severity] += 1
    return counts


def degraded(report: RunReport) -> bool:
    """Whether this run did less than it was configured to do.

    Note what is *not* here: a file shortened to the per-file limit. Truncation
    is the configured policy working normally, and almost every real repository
    has a file over 20,000 characters — counting it would make `degraded` true
    on nearly every run, which makes it useless as a signal and makes
    `fail-on-degraded` block everything. Trimmed files are still named in the
    summary and reported as their own output.

    A dropped file does count. Nothing about it was read.
    """
    return bool(
        report.scan_failures or report.verifier_error or report.dropped_files
    )


def exit_code(report: RunReport) -> int:
    """0 to let the merge through, 1 to block it.

    Returns 0 unless a gate was explicitly configured. Silently failing a build
    that never asked to be gated would be a worse surprise than any finding.
    """
    threshold = fail_on()
    if fail_on_degraded() and degraded(report):
        return 1
    if threshold == "never":
        return 0

    limit = SEVERITY_RANK[threshold]
    return int(
        any(SEVERITY_RANK.get(f.severity, 99) <= limit for f in posted(report))
    )


def gate_message(report: RunReport) -> str:
    """One line explaining a non-zero exit, or empty when the run passes."""
    if fail_on_degraded() and degraded(report):
        reasons = []
        if report.scan_failures:
            reasons.append(f"{len(report.scan_failures)} scanning model(s) failed")
        if report.verifier_error:
            reasons.append("the second opinion was unavailable")
        if report.dropped_files:
            reasons.append(f"{len(report.dropped_files)} file(s) were not reviewed")
        return (
            "failing because this review was degraded and fail-on-degraded is "
            f"set: {'; '.join(reasons)}"
        )

    threshold = fail_on()
    if threshold == "never":
        return ""

    limit = SEVERITY_RANK[threshold]
    blocking = [f for f in posted(report) if SEVERITY_RANK.get(f.severity, 99) <= limit]
    if not blocking:
        return ""
    worst = min(blocking, key=lambda f: SEVERITY_RANK.get(f.severity, 99))
    return (
        f"failing on {len(blocking)} finding(s) at or above {threshold}: "
        f"{worst.severity} in {worst.file_path} — {worst.title}"
    )


def write_outputs(report: RunReport) -> None:
    """Publish counts to ``$GITHUB_OUTPUT`` for later steps to branch on.

    Written even when a gate is not configured, so a team can start by
    observing — post to Slack when `critical` is non-zero, say — before
    deciding to block anything on it.
    """
    path = os.getenv("GITHUB_OUTPUT", "").strip()
    if not path:
        return

    counts = severity_counts(report)
    values = {
        "findings": str(len(posted(report))),
        "advisory": str(len(report.advisory)),
        "refuted": str(len(report.refuted)),
        "resolved": str(len(report.resolved)),
        "trimmed": str(len(report.trimmed_files)),
        "dropped": str(len(report.dropped_files)),
        "degraded": "true" if degraded(report) else "false",
        "repo-access": report.repo_access or "off",
        **{name: str(count) for name, count in counts.items()},
    }
    with pathlib.Path(path).open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def write_job_summary(body: str) -> None:
    """Put the review in the Actions run itself.

    Worth doing even though the same text goes on the pull request: this copy
    survives a posting failure, which is exactly when someone is looking at the
    run wondering what happened.
    """
    path = os.getenv("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    try:
        with pathlib.Path(path).open("a", encoding="utf-8") as handle:
            handle.write(body + "\n")
    except OSError as error:  # noqa: BLE001 - cosmetic; never fail a run for it
        print(f"note: could not write the job summary: {error}", file=sys.stderr)


def annotate(report: RunReport) -> None:
    """Surface degradation as an Actions warning.

    A green check on a run where one model never answered is the failure this
    project keeps coming back to. The summary comment says so, but nobody
    reading the checks list sees the summary comment.
    """
    for failure in report.scan_failures:
        print(f"::warning title=Scanning model failed::{failure}")
    if report.verifier_error:
        print(f"::warning title=Second opinion unavailable::{report.verifier_error}")
    if report.dropped_files:
        print(
            f"::warning title=Files not reviewed::"
            f"{len(report.dropped_files)} file(s) exceeded the diff budget"
        )
