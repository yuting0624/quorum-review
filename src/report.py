"""Rendering of the summary comment.

The summary is the only place a reader sees the two models side by side, which
matters more here than it would in a product: this repository exists to show a
cross-model arrangement, so the arrangement has to be visible in the output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schema import Finding

SEVERITY_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "⚪",
}


@dataclass
class RunReport:
    """Everything the summary comment needs to describe one run."""

    primary_model: str
    verifier_model: str = ""
    #: "ran", "disabled" (turned off to save cost), or "unavailable" (it broke).
    #: A reader needs to tell a deliberate single-model review from a degraded
    #: one, because only the second is a problem to go and fix.
    verifier_status: str = "ran"
    verifier_error: str = ""

    scanned: int = 0
    suppressed: int = 0
    confirmed: list[Finding] = field(default_factory=list)
    advisory: list[Finding] = field(default_factory=list)
    refuted: list[Finding] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    unanchored: list[Finding] = field(default_factory=list)
    trimmed_files: list[str] = field(default_factory=list)
    head_sha: str = ""


def _finding_row(finding: Finding) -> str:
    icon = SEVERITY_ICON.get(finding.severity, "⚪")
    location = f"`{finding.file_path}:{finding.line}`"
    return (
        f"| {icon} {finding.severity} | {finding.category} | {location} | "
        f"{finding.title} |"
    )


def _table(findings: list[Finding]) -> str:
    header = "| Severity | Category | Location | Finding |\n|---|---|---|---|"
    rows = "\n".join(_finding_row(finding) for finding in findings)
    return f"{header}\n{rows}"


def render(report: RunReport) -> str:
    """Build the Markdown body. The ledger marker is appended by the caller."""
    lines: list[str] = ["## Quorum review", ""]

    # How the two models were arranged, and what each contributed. This is the
    # part a reader is here for.
    if report.verifier_status == "ran":
        lines.append(
            f"`{report.primary_model}` scanned the diff and proposed "
            f"**{report.scanned}** finding(s). "
            f"`{report.verifier_model}` then judged each one independently: "
            f"**{len(report.confirmed)}** confirmed, "
            f"**{len(report.advisory)}** uncertain, "
            f"**{len(report.refuted)}** refuted."
        )
    else:
        lines.append(
            f"`{report.primary_model}` scanned the diff and proposed "
            f"**{report.scanned}** finding(s). Single-model review — these "
            f"findings have not been independently checked."
        )
        lines.append("")
        if report.verifier_status == "disabled":
            lines.append(
                "> ℹ️ **Verification is switched off for this repository.** "
                "Findings come from one model only, so expect more false "
                "positives than a verified review. Set `verification: on` to "
                "add a second opinion."
            )
        else:
            lines.append(
                f"> ⚠️ **Verification was supposed to run and could not.** "
                f"`{report.verifier_model}` was unavailable: "
                f"{report.verifier_error or 'reason not reported'}. "
                f"These findings are unfiltered."
            )

    if report.suppressed:
        lines.append("")
        lines.append(
            f"{report.suppressed} finding(s) were already reported earlier in "
            f"this pull request and are not repeated here."
        )

    if report.confirmed:
        lines += ["", "### Confirmed by both models", "", _table(report.confirmed)]

    if report.advisory:
        lines += [
            "",
            "### Advisory — not confirmed",
            "",
            "The verifier could not settle these from the diff alone, so they are "
            "listed here rather than posted inline.",
            "",
            _table(report.advisory),
        ]
        for finding in report.advisory:
            if finding.verifier_reason:
                lines.append(
                    f"- `{finding.file_path}:{finding.line}` — {finding.verifier_reason}"
                )

    if report.refuted:
        lines += [
            "",
            "<details>",
            f"<summary>Discarded by the verifier ({len(report.refuted)})</summary>",
            "",
        ]
        for finding in report.refuted:
            lines.append(
                f"- **{finding.title}** (`{finding.file_path}:{finding.line}`) — "
                f"{finding.verifier_reason or 'no reason given'}"
            )
        lines += ["", "</details>"]

    if report.resolved:
        lines += [
            "",
            "### Resolved since the last review",
            "",
        ]
        lines += [f"- {title}" for title in report.resolved]

    if report.unanchored:
        lines += [
            "",
            "### Could not be anchored to a line",
            "",
            "GitHub rejected an inline comment for these, usually because the "
            "line is outside the diff.",
            "",
        ]
        for finding in report.unanchored:
            lines.append(
                f"- `{finding.file_path}:{finding.line}` — **{finding.title}**: "
                f"{finding.body}"
            )

    if report.trimmed_files:
        lines += [
            "",
            "> Some files were truncated or skipped before review: "
            + ", ".join(f"`{path}`" for path in report.trimmed_files),
        ]

    if not (report.confirmed or report.advisory or report.resolved or report.suppressed):
        lines += ["", "No new issues found in this diff."]

    lines += [
        "",
        "---",
        "",
        f"<sub>Reviewed `{report.head_sha[:7]}` · primary `{report.primary_model}` · "
        f"verifier `{report.verifier_model or 'none'}` · quorum-review — a "
        f"reference implementation, not a supported product.</sub>",
    ]

    return "\n".join(lines)


def render_inline(finding: Finding) -> str:
    """Body of a single line-anchored comment."""
    icon = SEVERITY_ICON.get(finding.severity, "⚪")
    lines = [f"{icon} **{finding.title}**", "", finding.body]

    if finding.verifier_reason:
        lines += [
            "",
            "<details>",
            f"<summary>Confirmed independently by <code>{finding.verifier_model}</code>"
            f"</summary>",
            "",
            finding.verifier_reason,
            "",
            "</details>",
        ]

    lines += [
        "",
        f"<sub>`{finding.category}` · found by `{finding.primary_model}` · "
        f"id `{finding.finding_id}`</sub>",
    ]
    return "\n".join(lines)
