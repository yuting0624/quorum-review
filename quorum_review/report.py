"""Rendering of the summary comment.

The summary is the only place a reader sees how the models related to each
other, which matters more here than it would in a product: this repository
exists to show a cross-model arrangement, so the arrangement has to be legible
in the output. In particular, "both models found this without seeing each
other's work" and "one model found it and the other agreed when asked" are
different strengths of evidence, and the comment says which is which.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from . import redaction
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
    #: Titles of the findings suppressed as already reported. Matching is
    #: positional and by title overlap rather than exact, so it can be wrong;
    #: a count alone leaves a reader no way to notice that it was.
    suppressed_titles: list[str] = field(default_factory=list)
    agreed: list[Finding] = field(default_factory=list)
    confirmed: list[Finding] = field(default_factory=list)
    unverified: list[Finding] = field(default_factory=list)
    advisory: list[Finding] = field(default_factory=list)
    refuted: list[Finding] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    unanchored: list[Finding] = field(default_factory=list)
    summary_only: list[Finding] = field(default_factory=list)
    #: Findings that cleared the severity threshold but arrived after the run
    #: had already posted its allowance of inline comments. Listed in the
    #: summary rather than dropped.
    over_comment_cap: list[Finding] = field(default_factory=list)
    trimmed_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    #: Files the whole-diff budget left out entirely. Separate from
    #: `trimmed_files` because they mean different things to a reader: a
    #: trimmed file was partly reviewed, a dropped one was never looked at.
    dropped_files: list[str] = field(default_factory=list)
    #: Paths a model reported against that this change does not touch. Usually
    #: honest — it read the repository with tools and found something there —
    #: and occasionally a path the model invented outright. Either way the
    #: finding cannot be posted: there is no diff line to anchor it to. Named
    #: rather than counted, because silence here reads as "nothing was found in
    #: that file".
    off_diff_paths: list[str] = field(default_factory=list)
    #: ``{old path: new path}`` for files this change moves. Reported because a
    #: reader seeing findings appear at a path they did not edit deserves to
    #: know the reviewer followed a rename rather than invented them.
    renamed_files: dict[str, str] = field(default_factory=dict)
    usage: dict[str, ModelUsage] = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    incremental: bool = False
    since_sha: str = ""
    threads_not_collapsible: bool = False
    head_sha: str = ""

    #: Whether the models could read past the diff, and what they opened. Shown
    #: because a reader judging a finding needs to know what the reviewer had
    #: access to — "no guard anywhere" means one thing from a reviewer that
    #: searched the repository and another from one that saw eleven files.
    repo_access: str = ""
    #: Spend against the configured ceiling, when there is one.
    budget_note: str = ""
    #: How many findings were recovered from existing comments because the
    #: summary carrying the ledger had been deleted. Said out loud: a review
    #: that quietly lost its history and rebuilt part of it is not the same
    #: run as one that never lost anything.
    recovered: int = 0
    #: True when the marker was gone entirely rather than merely behind. The
    #: two are worth distinguishing: one loses severity and dismissal reasons,
    #: the other loses nothing.
    ledger_lost: bool = False
    #: What the checkout was at. Not the same object as the commit under
    #: review: `refs/pull/N/merge` is recomputed whenever the base branch
    #: moves, and resolved when the workflow checks out rather than when the
    #: diff was fetched, so the tree read can be a merge against a newer base.
    workspace_commit: str = ""
    tool_calls: int = 0
    files_read: list[str] = field(default_factory=list)


def evidence(finding: Finding) -> str:
    """One phrase describing how much agreement is behind this finding.

    Two models that never saw each other's output is stronger evidence than one
    model checked by another, which in turn beats one model unchecked. The
    column exists so a reader can weigh a finding without opening it.
    """
    if finding.agreed:
        return "both models, independently"
    reporter = cell(finding.reported_by[0]) if finding.reported_by else "?"
    if finding.verifier_model:
        return f"`{reporter}`, agreed by `{cell(finding.verifier_model)}`"
    if finding.reported_by:
        return f"`{reporter}`, unchecked"
    return "unknown"


#: A title longer than this is not a title. The schema asks for 80 characters
#: and models mostly comply, but a run that produces a paragraph would make one
#: row unreadable and push the rest of the table off the screen.
MAX_CELL_CHARS = 160

#: How many suppressed titles the summary lists before saying "and N more".
#: The visible body shares GitHub's 65,536-character comment limit with the
#: ledger marker, and overflowing it makes `fit_to_comment` drop the ledger's
#: history to make room — trading state that matters for a list that does not.
MAX_SUPPRESSED_LISTED = 20

#: A truncation can land inside `&amp;`, leaving `&am` to render as literal
#: text. Matches only an unterminated entity at the very end.
_PARTIAL_ENTITY = re.compile(r"&[a-z]{0,4}$")


def flatten(text: str, limit: int = MAX_CELL_CHARS) -> str:
    """One line, bounded, and inert as HTML.

    Three things, because a finding title is model output derived from a diff
    an attacker wrote, and it lands in the middle of Markdown:

    - **One line.** A newline inside ``**bold**`` ends the emphasis, and inside
      a list starts a new item mid-sentence.
    - **Bounded.** The schema asks for 80 characters; a run that returned a
      paragraph would make the summary unreadable.
    - **HTML-escaped.** Several sections are wrapped in ``<details>``, and
      GitHub renders raw HTML in comments. A title containing ``</details>``
      closes the block early and spills the rest of the summary out of it.
      Escaping is invisible to the reader: ``&lt;`` renders as ``<``.

    The ampersand goes first or ``<`` would become ``&amp;lt;``.

    Escaping happens *before* truncation, and that order is a correction: the
    other way round, a title of 160 ampersands passed the length check and then
    grew to 800 characters. Twenty-five of those is a fifth of GitHub's comment
    budget spent on nothing. The cost is that a cut can land inside an escape
    sequence, which ``_bound`` repairs.
    """
    return _bound(
        " ".join((text or "").split())
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;"),
        limit,
    )


def _bound(escaped: str, limit: int) -> str:
    """Cut to ``limit`` without leaving half an escape sequence behind.

    Two ways a naive cut goes wrong, and both were live at some point:

    - Inside ``&amp;``, leaving ``&am`` to render as literal text.
    - After an odd number of backslashes, so the next character — in a table,
      the closing pipe — gets escaped and the column disappears.

    The repairs expose each other, which is why this loops rather than
    applying them in some order. Stripping whitespace can uncover a backslash
    that was even a moment ago; dropping that backslash can uncover whitespace
    again. Two attempts to sequence these by hand were each wrong in a
    different direction, so the rule is now "apply all of them until nothing
    changes" — which terminates, because every step shortens the string.
    """
    if len(escaped) <= limit:
        return escaped

    cut = escaped[: limit - 1]
    while True:
        repaired = _PARTIAL_ENTITY.sub("", cut.rstrip()).rstrip()
        if (len(repaired) - len(repaired.rstrip("\\"))) % 2:
            repaired = repaired[:-1]
        if repaired == cut:
            return cut + "…"
        cut = repaired


def cell(text: str, limit: int = MAX_CELL_CHARS) -> str:
    r"""Make a value safe to put between two pipes.

    A security reviewer's titles contain pipes constantly — `cmd | grep`,
    `a || b`, regex alternation. An unescaped one adds a column, so GitHub
    shifts every later cell left and the Evidence column, which is the whole
    argument this table exists to make, shows a fragment of the title instead.
    A newline is worse: it ends the table and dumps the rest as prose.

    Backslashes go first, and the order is the bug this had on its first
    attempt: escaping only the pipe turns ``a\|b`` into ``a\\|b``, which
    Markdown reads as a literal backslash followed by an unescaped pipe — the
    column break the escaping was for. And ``\|`` is not exotic; it is how you
    write an escaped pipe in a regular expression, which is a thing findings
    quote.

    Both breakages were live. Neither shows up in a test that only checks the
    text appears somewhere in the row.

    The bound is applied after every escape, not by delegating to ``flatten``:
    doing it there and then escaping here doubled a cell of backslashes or
    pipes right back over the limit. Escaping is not length-preserving, so the
    cut has to come last.
    """
    escaped = (
        " ".join((text or "").split())
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\\", "\\\\")
        .replace("|", r"\|")
    )
    return _bound(escaped, limit)


def _row(finding: Finding) -> str:
    icon = SEVERITY_ICON.get(finding.severity, "⚪")
    return (
        f"| {icon} {cell(finding.severity)} | {cell(finding.category)} | "
        f"`{cell(finding.file_path)}:{finding.line}` | {cell(finding.title)} | "
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
        "| Severity | Category | Location | Finding | Evidence |\n|---|---|---|---|---|"
    )
    return "\n".join([header, *(_row(finding) for finding in ordered)])


def _counts(findings: list[Finding]) -> str:
    """A one-line severity tally, so the headline does not need the table."""
    tally = Counter(finding.severity for finding in findings)
    parts = [f"{tally[name]} {name}" for name in SEVERITIES if tally.get(name)]
    return ", ".join(parts)


def _repo_access(report: RunReport) -> list[str]:
    """What the models read beyond the diff — or why they could not.

    Worth its own paragraph rather than a footnote. Whether a reviewer could
    open the function it is complaining about changes how much its silence and
    its confidence are each worth, and the reader cannot infer that from the
    findings themselves.
    """
    if report.repo_access.startswith("off"):
        return [
            "",
            f"> ℹ️ **Diff only** ({report.repo_access.removeprefix('off: ')}). "
            f"The models could not read files outside the diff, so anything "
            f"that turns on code elsewhere — a validator, a permission "
            f"registry, a function signature — was judged from the call site "
            f"alone.",
        ]
    if not report.repo_access:
        return []

    if not report.files_read:
        return [
            "",
            f"Both models could read the repository; neither needed to "
            f"({report.tool_calls} lookup(s), no files opened).",
        ]

    shown = ", ".join(f"`{path}`" for path in report.files_read[:8])
    if len(report.files_read) > 8:
        shown += f", and {len(report.files_read) - 8} more"
    # Which tree, not just which files. A merge ref is recomputed when the base
    # branch moves, so it can differ from the commit the diff describes — and
    # someone chasing a finding that does not match their checkout deserves to
    # know which one the reviewer read.
    where = f" at `{report.workspace_commit}`" if report.workspace_commit else ""
    return [
        "",
        f"Beyond the diff, the models made **{report.tool_calls}** read-only "
        f"lookup(s) into the checkout{where} and opened {shown}.",
    ]


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

    lines += _repo_access(report)

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
        if report.suppressed_titles:
            # Bounded, because the summary shares GitHub's 65,536-character
            # limit with the ledger marker. Overflowing it makes
            # `fit_to_comment` drop the ledger's history to make room for a
            # list nobody reads, which trades state for noise.
            shown = report.suppressed_titles[:MAX_SUPPRESSED_LISTED]
            remainder = len(report.suppressed_titles) - len(shown)
            lines += [
                "",
                "<details>",
                "<summary>Which ones</summary>",
                "",
                *(f"- {flatten(title)}" for title in shown),
                *([f"- …and {remainder} more"] if remainder else []),
                "",
                "Matched to an existing finding by position and wording, not by "
                "an exact identifier — models do not quote the same defect the "
                "same way twice. Listed so a wrong match is visible rather than "
                "silent.",
                "",
                "</details>",
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
                f"- **{flatten(finding.title)}** "
                f"(`{finding.file_path}:{finding.line}`) — "
                f"{flatten(finding.verifier_reason, 400) or 'no reason given'}"
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
            "Not raised by the last two reviews of these files. Two, rather "
            "than one, because models are not perfectly repeatable and a "
            "single scan disagreeing with the previous one is ordinary — "
            "closing on the first miss made findings flap. Still not a "
            "guarantee of a fix: they will reappear if a later review finds "
            "them again.",
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
                f"- `{finding.file_path}:{finding.line}` — "
                f"**{flatten(finding.title)}**: {finding.body}"
            )

    if report.summary_only:
        lines += [
            "",
            f"<sub>{len(report.summary_only)} finding(s) are listed above but "
            f"not commented inline, being below the configured severity "
            f"threshold.</sub>",
        ]

    if report.over_comment_cap:
        lines += [
            "",
            f"> ℹ️ **{len(report.over_comment_cap)} further finding(s) are in "
            f"the table above but were not commented inline.** This run had "
            f"already used its allowance of inline comments; a pull request "
            f"buried under them is one nobody reads. They are not dismissed, "
            f"and the most severe findings got the comments. Raise "
            f"`max-inline-comments`, or split the change.",
        ]

    if report.trimmed_files:
        lines += [
            "",
            "> Too large to send whole, so only partly reviewed: "
            + ", ".join(f"`{path}`" for path in report.trimmed_files),
        ]

    if report.dropped_files:
        shown = ", ".join(f"`{p}`" for p in report.dropped_files[:10])
        if len(report.dropped_files) > 10:
            shown += f", and {len(report.dropped_files) - 10} more"
        lines += [
            "",
            f"> ⚠️ **{len(report.dropped_files)} file(s) were not reviewed.** "
            f"The diff exceeded the size budget, and these did not fit: {shown}. "
            f"Nothing below is a statement about them. Split the pull request, "
            f"or raise `max-diff-characters`.",
        ]

    if report.off_diff_paths:
        # `flatten`, unlike every other path list here. Those come from the
        # diff; this one is the model's own text, and it is on this list
        # *because* it did not match anything GitHub sent. Echoing it raw put
        # attacker-influenced content into the same comment that carries
        # `<!-- quorum-state: ... -->`, where a forged marker would be read
        # back as the ledger on the next run.
        shown = ", ".join(f"`{flatten(p, 200)}`" for p in report.off_diff_paths[:10])
        if len(report.off_diff_paths) > 10:
            shown += f", and {len(report.off_diff_paths) - 10} more"
        lines += [
            "",
            f"> Findings were raised against {len(report.off_diff_paths)} path(s) "
            f"this change does not touch, and were dropped: {shown}. A model "
            f"reading the repository can find real problems outside the diff, "
            f"but there is no line here to attach a comment to. If one of these "
            f"looks like a file you did edit, the path is wrong.",
        ]

    if report.recovered and report.ledger_lost:
        lines += [
            "",
            f"> ℹ️ **The summary comment carrying this review's state had been "
            f"deleted.** {report.recovered} finding(s) were recovered from the "
            f"comments still on this pull request, so they are not reported "
            f"again. What could not be recovered: severity, which model raised "
            f"each one, and the reason behind any dismissal.",
        ]
    elif report.recovered:
        lines += [
            "",
            f"> ℹ️ **{report.recovered} finding(s) were already on this pull "
            f"request but missing from its recorded state**, and have been "
            f"taken back in rather than posted again. The usual cause is a run "
            f"cancelled between posting its comments and saving the record of "
            f"them.",
        ]

    if report.renamed_files:
        moves = ", ".join(
            f"`{old}` → `{new}`" for old, new in list(report.renamed_files.items())[:5]
        )
        if len(report.renamed_files) > 5:
            moves += f", and {len(report.renamed_files) - 5} more"
        lines += [
            "",
            f"<sub>Followed {len(report.renamed_files)} rename(s) — {moves} — so "
            f"findings already tracked in those files were not closed and "
            f"re-opened.</sub>",
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
        # Deliberately not "looks good": the reviewer read a bounded slice of
        # one change, and a clean result on a diff that was cut short is not
        # the same statement as a clean result on all of it.
        lines += [
            "",
            "No new issues found in the diff that was reviewed."
            if report.dropped_files
            else "No new issues found in this diff.",
        ]

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


def _version() -> str:
    """Which reviewer produced this.

    Findings outlive the run that made them — in a comment, in the Security tab,
    in someone's quarterly report — and "why did it say that in March" is not
    answerable without knowing what was running in March. The models are already
    named; this is the other half.
    """
    from . import __version__

    return __version__


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

    if report.budget_note:
        lines.append(f"<sub>Token ceiling: {report.budget_note}.</sub>")
        lines.append("")

    elapsed = f" · {report.elapsed_seconds:.0f}s" if report.elapsed_seconds else ""
    lines.append(
        f"<sub>Reviewed `{report.head_sha[:7]}` · quorum-review "
        f"`{_version()}` · models "
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
    lines = [f"{icon} **{flatten(finding.title)}**", "", finding.body]

    # Redaction happened at the source, in review.py, not here. There are five
    # places a finding's text reaches a comment — including the ledger, which
    # lives inside the summary — and doing it per site works until someone adds
    # the sixth.
    if with_suggestion and finding.fix_replacement:
        lines += ["", "```suggestion", finding.fix_replacement.rstrip("\n"), "```"]

    if finding.agreed:
        lines += [
            "",
            "> **Found by two models independently** — "
            + " and ".join(f"`{m}`" for m in finding.reported_by)
            + " each read this diff without seeing the other's output.",
        ]
    elif finding.verifier_reason:
        reporter = finding.reported_by[0] if finding.reported_by else "one model"
        lines += [
            "",
            "<details>",
            f"<summary>Second opinion from <code>{finding.verifier_model}</code>"
            f"</summary>",
            "",
            f"`{reporter}` raised this. `{finding.verifier_model}` was then asked "
            f"to judge it — without being shown the reasoning, the severity, or "
            f"who reported it, so that it would assess the code rather than "
            f"agree with a colleague. Its answer:",
            "",
            f"> {finding.verifier_reason}",
            "",
            "</details>",
        ]

    if finding.redacted:
        lines += ["", redaction.note(finding.redacted)]

    lines += [
        "",
        f"<sub>`{finding.category}` · id `{finding.finding_id}`</sub>",
    ]
    return "\n".join(lines)


def render_nothing_to_review(report: RunReport, skipped: list[str]) -> str:
    """The summary for a pull request with no reviewable diff.

    Reachable more often than it sounds — a change that only touches lockfiles
    or generated code, or an incremental re-review whose new commits are all
    excluded. It gets its own rendering rather than the ordinary "no new issues
    found", because those two sentences mean opposite things: one is a review
    that looked and found nothing, the other is a review that did not look.
    """
    lines = ["## Quorum review", ""]

    if skipped:
        lines.append(
            f"Nothing to review: all {len(skipped)} changed file(s) are "
            f"excluded as generated, vendored, or configured out."
        )
        lines += ["", _skipped_note(skipped)]
    else:
        lines.append(
            "Nothing to review: this change contains no reviewable text. "
            "That is usually a binary-only or empty diff."
        )

    lines += [
        "",
        "No models were called. **This is not a clean review** — nothing was examined.",
        "",
        "---",
        "",
        f"<sub>Reviewed `{report.head_sha[:7]}` · quorum-review "
        f"`{_version()}` · quorum-review — a reference implementation, not a "
        f"supported product.</sub>",
    ]
    return "\n".join(lines)
