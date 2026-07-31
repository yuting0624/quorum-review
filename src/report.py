"""Rendering of the summary comment.

The summary is the only place a reader sees how the models related to each
other, which matters more here than it would in a product: this repository
exists to show a cross-model arrangement, so the arrangement has to be legible
in the output. In particular, "both models found this without seeing each
other's work" and "one model found it and the other agreed when asked" are
different strengths of evidence, and the comment says which is which.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .schema import SEVERITIES, SEVERITY_RANK, Finding, ModelUsage

SEVERITY_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "⚪",
}


@dataclass
class RunReport:
    """Everything the summary comment needs to describe one run."""

    models: list[str] = field(default_factory=list)
    scanning_models: list[str] = field(default_factory=list)
    verification_on: bool = True
    verifier_error: str = ""
    scan_failures: list[str] = field(default_factory=list)
    per_model_counts: dict[str, int] = field(default_factory=dict)

    scanned: int = 0
    suppressed: int = 0
    agreed: list[Finding] = field(default_factory=list)
    confirmed: list[Finding] = field(default_factory=list)
    unverified: list[Finding] = field(default_factory=list)
    advisory: list[Finding] = field(default_factory=list)
    refuted: list[Finding] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    unanchored: list[Finding] = field(default_factory=list)
    summary_only: list[Finding] = field(default_factory=list)
    trimmed_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    usage: dict[str, ModelUsage] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    incremental: bool = False
    since_sha: str = ""
    threads_not_collapsible: bool = False
    head_sha: str = ""


def evidence(finding: Finding) -> str:
    """One phrase describing why this finding is being shown."""
    if finding.agreed:
        return "both models, independently"
    if finding.verifier_model:
        return f"`{finding.reported_by[0] if finding.reported_by else '?'}`, " + (
            f"checked by `{finding.verifier_model}`"
        )
    if finding.reported_by:
        return f"`{finding.reported_by[0]}`, unchecked"
    return "unknown"


def _row(finding: Finding) -> str:
    icon = SEVERITY_ICON.get(finding.severity, "⚪")
    return (
        f"| {icon} {finding.severity} | {finding.category} | "
        f"`{finding.file_path}:{finding.line}` | {finding.title} | "
        f"{evidence(finding)} |"
    )


def _table(findings: list[Finding]) -> str:
    """Render a findings table, worst first.

    Reading order is the only prioritisation a summary can offer, so it should
    not be the order the models happened to return.
    """
    ordered = sorted(
        findings,
        key=lambda f: (SEVERITY_RANK.get(f.severity, 99), f.file_path, f.line),
    )
    header = (
        "| Severity | Category | Location | Finding | Evidence |\n"
        "|---|---|---|---|---|"
    )
    return "\n".join([header, *(_row(finding) for finding in ordered)])


def _counts(findings: list[Finding]) -> str:
    """A one-line severity tally, so the headline does not need the table."""
    tally = Counter(finding.severity for finding in findings)
    parts = [
        f"{tally[name]} {name}" for name in SEVERITIES if tally.get(name)
    ]
    return ", ".join(parts)


def _how_it_ran(report: RunReport) -> list[str]:
    """The paragraph explaining what each model did. The point of the report."""
    lines: list[str] = []
    scanners = ", ".join(f"`{m}`" for m in report.scanning_models)

    if len(report.scanning_models) > 1:
        counts = " and ".join(
            f"`{model}` {count}" for model, count in report.per_model_counts.items()
        )
        lines.append(
            f"{scanners} each read the diff without seeing the other's output "
            f"({counts}), which merged to **{report.scanned}** distinct "
            f"finding(s). **{len(report.agreed)}** of those were reported by "
            f"both models independently."
        )
        if report.verification_on:
            checked = len(report.confirmed) - len(report.agreed)
            lines.append(
                f"The remaining findings were each judged by the model that did "
                f"*not* report them: **{max(checked, 0)}** confirmed, "
                f"**{len(report.advisory)}** uncertain, "
                f"**{len(report.refuted)}** refuted."
            )
    else:
        lines.append(
            f"{scanners} read the diff and proposed **{report.scanned}** finding(s)."
        )
        if report.verification_on:
            lines.append(
                f"Each was then judged independently: "
                f"**{len(report.confirmed)}** confirmed, "
                f"**{len(report.advisory)}** uncertain, "
                f"**{len(report.refuted)}** refuted."
            )

    if not report.verification_on:
        lines += [
            "",
            "> ℹ️ **Single-opinion review.** Findings only one model reported "
            "were not checked by the other. Cheaper, but expect more false "
            "positives. Enable `verification` to add the second pass.",
        ]

    if len(report.scanning_models) == 1 and len(report.models) > 1:
        lines += [
            "",
            "> ℹ️ **Only one model scanned this diff.** Anything it missed was "
            "not looked for by the other — verification judges reported "
            "findings, it does not search for new ones. Set `scan: both` to "
            "raise recall.",
        ]

    if report.verifier_error:
        lines += [
            "",
            f"> ⚠️ **A verifier could not run:** {report.verifier_error}. "
            f"Affected findings are listed as uncertain rather than dropped.",
        ]

    if report.scan_failures:
        lines += [
            "",
            "> ⚠️ **A scanning model failed:** "
            + "; ".join(report.scan_failures)
            + ". This review saw less than it should have.",
        ]

    return lines


def render(report: RunReport) -> str:
    """Build the Markdown body. The ledger marker is appended by the caller."""
    lines: list[str] = ["## Quorum review", ""]

    if report.incremental:
        lines += [
            f"Reviewing only what changed since `{report.since_sha[:7]}`. Findings "
            f"in files this range does not touch are carried over untouched — "
            f"they were not re-examined, so they are neither re-reported nor "
            f"treated as fixed.",
            "",
        ]

    lines += _how_it_ran(report)

    if report.suppressed:
        lines += [
            "",
            f"{report.suppressed} finding(s) were already reported earlier in "
            f"this pull request and are not repeated here.",
        ]

    if report.confirmed:
        lines += [
            "",
            f"### Confirmed — {_counts(report.confirmed)}",
            "",
            _table(report.confirmed),
        ]

    if report.unverified:
        lines += [
            "",
            f"### Reported, not independently checked — {_counts(report.unverified)}",
            "",
            _table(report.unverified),
        ]

    if report.advisory:
        lines += [
            "",
            "### Advisory — not confirmed",
            "",
            "The second model could not settle these from the diff alone, so "
            "they are listed here rather than posted inline.",
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
            f"<summary>Refuted by the other model ({len(report.refuted)})</summary>",
            "",
        ]
        for finding in report.refuted:
            lines.append(
                f"- **{finding.title}** (`{finding.file_path}:{finding.line}`) — "
                f"{finding.verifier_reason or 'no reason given'}"
            )
        lines += ["", "</details>"]

    if report.resolved:
        # Deliberately not called "resolved". A finding drops off this run
        # either because it was fixed or because the scan did not re-detect it,
        # and from here the two are indistinguishable. Claiming a fix that did
        # not happen is the worse error, so the wording states only what is
        # actually known.
        lines += [
            "",
            "### No longer reported",
            "",
            "Previously flagged and not raised by this review — either fixed, or "
            "not re-detected. These are no longer suppressed, so they will "
            "reappear if a later review finds them again.",
            "",
        ]
        lines += [f"- {title}" for title in report.resolved]

        if report.threads_not_collapsible:
            lines += [
                "",
                "> ℹ️ Their threads were replied to but left open: the default "
                "`GITHUB_TOKEN` is not allowed to resolve review threads, "
                "whatever `permissions:` says. Supply a GitHub App token via "
                "`github-token` to have them collapse automatically.",
            ]

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

    if report.summary_only:
        lines += [
            "",
            f"<sub>{len(report.summary_only)} finding(s) are listed above but "
            f"not commented inline, being below the configured severity "
            f"threshold.</sub>",
        ]

    if report.trimmed_files:
        lines += [
            "",
            "> Too large to send whole, so only partly reviewed: "
            + ", ".join(f"`{path}`" for path in report.trimmed_files),
        ]

    if report.skipped_files:
        lines += ["", _skipped_note(report.skipped_files)]

    if not (
        report.confirmed
        or report.unverified
        or report.advisory
        or report.resolved
        or report.suppressed
    ):
        lines += ["", "No new issues found in this diff."]

    lines += ["", "---", "", *_footer(report)]
    return "\n".join(lines)


def _skipped_note(paths: list[str]) -> str:
    """Say what was not reviewed, without burying the findings under a list."""
    shown = ", ".join(f"`{path}`" for path in paths[:8])
    more = f" and {len(paths) - 8} more" if len(paths) > 8 else ""
    return (
        f"<sub>Not reviewed — generated, vendored, or excluded "
        f"({len(paths)} file(s)): {shown}{more}. Adjust with the `exclude` "
        f"input or a `.quorumignore` file.</sub>"
    )


def _footer(report: RunReport) -> list[str]:
    """Provenance and cost.

    Tokens and call counts rather than a monetary figure: prices differ by
    model, platform, and contract, so a number computed here would be a guess
    wearing the costume of a fact.
    """
    lines = []

    if report.usage:
        rows = [
            "| Model | Calls | Input | Cached input | Output |",
            "|---|--:|--:|--:|--:|",
        ]
        for model, used in report.usage.items():
            rows.append(
                f"| `{model}` | {used.calls} | {used.input_tokens:,} | "
                f"{used.cached_input_tokens:,} | {used.output_tokens:,} |"
            )
        lines += ["<details>", "<summary>Usage</summary>", ""]
        lines += [*rows, "", "</details>", ""]

    elapsed = f" · {report.elapsed_seconds:.0f}s" if report.elapsed_seconds else ""
    lines.append(
        f"<sub>Reviewed `{report.head_sha[:7]}` · models "
        + ", ".join(f"`{m}`" for m in report.models)
        + f"{elapsed} · quorum-review — a reference implementation, not a "
        "supported product.</sub>"
    )
    return lines


def render_inline(finding: Finding, with_suggestion: bool = True) -> str:
    """Body of a single line-anchored comment.

    ``with_suggestion`` is turned off when GitHub rejects the multi-line anchor
    a suggestion needs. The finding is still worth posting; the one-click fix
    is what gets dropped, because a suggestion applied to the wrong range would
    corrupt the file.
    """
    icon = SEVERITY_ICON.get(finding.severity, "⚪")
    lines = [f"{icon} **{finding.title}**", "", finding.body]

    if with_suggestion and finding.fix_replacement:
        lines += ["", "```suggestion", finding.fix_replacement.rstrip("\n"), "```"]

    if finding.agreed:
        lines += [
            "",
            "> Reported independently by "
            + " and ".join(f"`{m}`" for m in finding.reported_by)
            + ". Neither model saw the other's output.",
        ]
    elif finding.verifier_reason:
        lines += [
            "",
            "<details>",
            f"<summary>Checked by <code>{finding.verifier_model}</code>, which did "
            f"not report it</summary>",
            "",
            finding.verifier_reason,
            "",
            "</details>",
        ]

    lines += [
        "",
        f"<sub>`{finding.category}` · id `{finding.finding_id}`</sub>",
    ]
    return "\n".join(lines)
