"""Orchestrator: independent scans -> consensus -> verify the difference -> post.

Run inside GitHub Actions as ``python -m src.review``.

Both models read the diff without seeing each other's output. Where they agree
independently, that agreement is the result and no further call is made. Where
only one reported something, the other is asked to judge it. This is what keeps
one model's blind spot from becoming the whole reviewer's blind spot —
verification alone cannot do that, because it never sees a finding that was
never reported.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import pathlib
import sys
import time

from . import actions, consensus, conversation, dismissal, forks, learning
from . import ledger as ledger_mod
from . import report as report_mod
from . import workspace as workspace_mod
from .github_client import GitHubClient, GitHubError, pr_number_from_event, read_event
from .providers import ProviderUnavailable, build_provider
from .providers.base import ReviewProvider
from .schema import SEVERITY_RANK, Finding, PRContext, Skill
from .workspace import Workspace

#: Verification costs one model call per unresolved finding, so it is capped.
#: Findings are ranked by severity first, so the cap drops the least important.
MAX_VERIFIED_FINDINGS = int(os.getenv("MAX_VERIFIED_FINDINGS", "20"))

#: Vertex has no Batches API, so concurrency is the only way to keep the
#: verification stage from running serially.
VERIFY_CONCURRENCY = int(os.getenv("VERIFY_CONCURRENCY", "4"))

#: Whole-run ceiling. A hung model call must not hold an Actions runner open.
TIMEOUT_SECONDS = int(os.getenv("QUORUM_TIMEOUT_SECONDS", "1200"))

SKILLS_ROOT = pathlib.Path(__file__).resolve().parent.parent / "skills"

_OFF = {"off", "0", "false", "no"}


def verification_enabled() -> bool:
    """Whether findings only one model reported get a second opinion.

    Read at call time rather than at import so tests and the benchmark harness
    can flip it.
    """
    return os.getenv("QUORUM_VERIFICATION", "on").strip().lower() not in _OFF


def incremental_enabled() -> bool:
    """Whether a re-review reads only what changed since the last one.

    On by default. The saving compounds: without it, a pull request on its
    tenth commit re-reads all ten commits' worth of diff on every run, and
    re-derives findings the ledger already knows about.
    """
    return os.getenv("QUORUM_INCREMENTAL", "on").strip().lower() not in _OFF


def scan_with_all_models() -> bool:
    """Whether every configured model scans, or only the first.

    Scanning with both is what raises recall, and it costs one extra call
    regardless of diff size — but it does mean paying for two models on every
    pull request. ``QUORUM_SCAN=single`` is the cheap tier.
    """
    return os.getenv("QUORUM_SCAN", "both").strip().lower() not in {"single", "one", "1"}


#: Tool calls one verification may make. Far below a scan's budget: a verifier
#: is settling one specific claim, and it is called once per finding.
VERIFY_TOOL_CALLS = int(os.getenv("QUORUM_VERIFY_TOOL_CALLS", "6"))


def inline_min_severity() -> str:
    """Lowest severity that earns its own inline comment.

    Everything still appears in the summary. This only decides what interrupts
    the reader in the diff view: a dozen simultaneous comments on one pull
    request is how a reviewer gets muted.
    """
    value = os.getenv("QUORUM_INLINE_SEVERITY", "low").strip().lower()
    return value if value in SEVERITY_RANK else "low"


def load_skill(name: str) -> Skill:
    path = SKILLS_ROOT / name / "SKILL.md"
    if not path.exists():
        available = ", ".join(sorted(p.name for p in SKILLS_ROOT.iterdir() if p.is_dir()))
        raise FileNotFoundError(f"unknown skill {name!r}; available: {available}")
    return Skill(name=name, content=path.read_text(encoding="utf-8"))


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Collapse findings that resolved to the same ID.

    A model asked for recall will sometimes report one root cause twice with
    different wording; identical IDs mean identical code, so the duplicate adds
    nothing.
    """
    seen: dict[str, Finding] = {}
    for finding in findings:
        seen.setdefault(finding.finding_id, finding)
    return list(seen.values())


def by_severity(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: SEVERITY_RANK.get(f.severity, 99))


async def scan_all(
    provider: ReviewProvider,
    models: list[str],
    ctx: PRContext,
    skill: Skill,
    workspaces: list[Workspace | None] | None = None,
) -> tuple[list[list[Finding]], list[str]]:
    """Run every model over the diff concurrently and independently.

    A model that fails does not sink the review: its results are simply absent,
    and the summary names it so nobody mistakes a degraded run for a clean one.
    """
    budgets = workspaces or [None] * len(models)

    async def one(model: str, toolbox: Workspace | None) -> list[Finding]:
        return await provider.scan(model, ctx, skill, toolbox=toolbox)

    results = await asyncio.gather(
        *(one(model, toolbox) for model, toolbox in zip(models, budgets, strict=True)),
        return_exceptions=True,
    )

    scans: list[list[Finding]] = []
    failures: list[str] = []
    for model, result in zip(models, results, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{model}: {result}")
        else:
            scans.append(result)
    return scans, failures


async def verify_all(
    provider: ReviewProvider,
    findings: list[Finding],
    models: list[str],
    ctx: PRContext,
    workspaces: list[Workspace | None] | None = None,
) -> tuple[list[Finding], str]:
    """Judge each unresolved finding with a model that did not report it.

    Returns the findings with verdicts attached, and an error string that is
    non-empty when a verifier could not run at all. A per-finding failure is
    absorbed as ``uncertain`` — one bad call should not sink the review.
    """
    semaphore = asyncio.Semaphore(VERIFY_CONCURRENCY)
    unavailable: list[str] = []
    budgets = workspaces or [None] * len(findings)

    async def one(finding: Finding, toolbox: Workspace | None) -> Finding:
        reviewer = consensus.reviewer_for(finding, models)
        if reviewer is None:
            finding.verdict = "confirmed"
            finding.verifier_reason = "no other model was available to check this"
            return finding

        async with semaphore:
            try:
                verdict = await provider.verify(reviewer, finding, ctx, toolbox=toolbox)
            except ProviderUnavailable as error:
                unavailable.append(str(error))
                finding.verdict = "uncertain"
                finding.verifier_reason = "the verifier was unavailable"
                return finding
            except Exception as error:  # noqa: BLE001 - degrade, don't abort
                finding.verdict = "uncertain"
                finding.verifier_reason = f"verification failed: {error}"
                return finding

        finding.verdict = verdict.verdict
        finding.verifier_reason = verdict.reason
        finding.verifier_model = verdict.model
        # The verifier re-scores severity on its own; its rating wins because it
        # looked at the code without seeing the original claim's rating.
        if verdict.severity:
            finding.severity = verdict.severity
        return finding

    verified = await asyncio.gather(
        *(
            one(finding, toolbox)
            for finding, toolbox in zip(findings, budgets, strict=True)
        )
    )
    return list(verified), unavailable[0] if unavailable else ""


def wants_criteria_proposal(event: dict) -> bool:
    """Whether someone asked for the dismissals to be turned into criteria."""
    comment = event.get("comment")
    if not isinstance(comment, dict):
        return False
    return "@quorum /criteria" in (comment.get("body") or "").lower()


async def propose_criteria(github: GitHubClient, number: int, skill_name: str) -> int:
    """Summarise dismissed findings into a suggested change to the criteria.

    Posted as a comment for a human to apply, never written to the skill file.
    Dismissal reasons come from pull request comments, so applying them
    automatically would let someone argue the reviewer out of a category of
    finding by dismissing it convincingly a few times.
    """
    ledger, _sticky = await github.load_ledger(number)
    entries = learning.dismissed(ledger)

    if len(entries) < learning.MIN_DISMISSALS:
        await github.post_issue_comment(
            number,
            f"Only {len(entries)} dismissed finding(s) on this pull request. "
            f"I need at least {learning.MIN_DISMISSALS} before a pattern is "
            f"worth reading anything into — narrowing the criteria for a "
            f"one-off costs more than it saves.",
        )
        return 0

    skill = load_skill(skill_name)
    provider = build_provider()
    body = await provider.respond(
        provider.models[0],
        "You improve code review criteria based on feedback from maintainers.",
        learning.render_prompt(skill.name, skill.content, entries),
    )

    proposal = learning.Proposal(skill=skill.name, dismissals=entries, body=body.strip())
    await github.post_issue_comment(number, learning.render_comment(proposal))
    return 0


async def post_finding(
    github: GitHubClient, number: int, head_sha: str, finding: Finding
) -> int | None:
    """Post one finding inline, degrading rather than losing it.

    A one-click suggestion has to be anchored to exactly the lines it replaces,
    and GitHub rejects a range that is not wholly inside the diff. When that
    happens the finding is still worth saying — so it is retried on the single
    line, with the suggestion dropped. Keeping the suggestion while narrowing
    the anchor would apply the replacement to the wrong lines.
    """
    spans_range = bool(finding.fix_replacement) and finding.fix_end_line > finding.line

    comment_id = await github.post_inline_comment(
        number=number,
        commit_sha=head_sha,
        path=finding.file_path,
        line=finding.fix_end_line if spans_range else finding.line,
        start_line=finding.line if spans_range else None,
        body=report_mod.render_inline(finding),
    )
    if comment_id is not None or not spans_range:
        return comment_id

    return await github.post_inline_comment(
        number=number,
        commit_sha=head_sha,
        path=finding.file_path,
        line=finding.line,
        body=report_mod.render_inline(finding, with_suggestion=False),
    )


async def close_threads(
    github: GitHubClient,
    number: int,
    entries: list[ledger_mod.LedgerEntry],
    head_sha: str,
) -> bool:
    """Reply to and collapse the threads of findings that are no longer raised.

    Leaving them open is the failure mode this project was built partly in
    reaction to: a reviewer that keeps a wall of resolved comments open teaches
    people to stop reading it.

    Returns True when collapsing was refused. The default ``GITHUB_TOKEN``
    cannot call ``resolveReviewThread`` — GitHub forbids it for the Actions app
    whatever `permissions:` says — so this is an ordinary configuration state,
    not a bug, and the summary explains it rather than the logs swallowing it.
    The reply is still posted either way.
    """
    anchored = [entry for entry in entries if entry.review_comment_id]
    if not anchored:
        return False

    try:
        threads = await github.review_threads(number)
    except GitHubError as error:
        print(f"warning: could not read review threads: {error}", file=sys.stderr)
        return False

    forbidden = False
    for entry in anchored:
        comment_id = int(entry.review_comment_id or 0)
        thread = threads.get(comment_id)
        if thread is None or thread.is_resolved:
            continue

        try:
            await github.reply_to_comment(
                number,
                comment_id,
                f"No longer reported as of `{head_sha[:7]}`. It will reappear "
                f"if a later review finds it again.",
            )
        except GitHubError as error:
            print(f"warning: could not reply to {comment_id}: {error}", file=sys.stderr)
            continue

        if forbidden:
            continue  # the token cannot resolve; do not ask N more times
        try:
            await github.resolve_thread(thread.thread_id)
        except GitHubError as error:
            forbidden = True
            print(f"note: threads cannot be collapsed by this token: {error}",
                  file=sys.stderr)

    return forbidden


async def run(skill_name: str, dry_run: bool) -> int:
    event = read_event()
    number = pr_number_from_event(event)

    # Retiring a false positive is a state change, not a review. Running a full
    # review here would burn two model calls to answer a question nobody asked.
    if dismissal.is_dismissal(event):
        async with GitHubClient() as github:
            return await dismissal.handle(github, event, number)

    # Turning accumulated dismissals into a criteria proposal is one call and
    # posts a suggestion, so it is not a review either.
    if wants_criteria_proposal(event):
        async with GitHubClient() as github:
            return await propose_criteria(github, number, skill_name)

    # A question is one call to the model that made the claim, not a re-review.
    if conversation.is_question(event):
        async with GitHubClient() as github:
            ledger, _ = await github.load_ledger(number)
            ctx, _skipped, _trimmed, _dropped = await github.load_context(
                number, exclude_input=os.getenv("QUORUM_EXCLUDE", "")
            )
            return await conversation.handle(
                github, build_provider(), ctx, ledger, event
            )

    skill = load_skill(skill_name)

    provider = build_provider()
    models = list(provider.models)
    scanning = models if scan_with_all_models() else models[:1]
    verify_wanted = verification_enabled() and len(models) > 1

    started = time.monotonic()

    async with GitHubClient() as github:
        # Forks are re-checked here even though the workflow already gated on
        # the label. A workflow condition is one careless edit away from being
        # wrong, and under `pull_request_target` the consequence of being wrong
        # is a run with write access authorised by a stranger.
        from_fork = await forks.is_fork(github, number, event)
        if from_fork:
            refusal = await forks.refusal(github, number, event)
            if refusal:
                print(f"::notice title=Not reviewed::{refusal}")
                print(f"skipped: {refusal}", file=sys.stderr)
                return 0

        ledger, sticky = await github.load_ledger(number)
        ctx, skipped_files, trimmed, dropped = await github.load_context(
            number,
            exclude_input=os.getenv("QUORUM_EXCLUDE", ""),
            since_sha=ledger.last_reviewed_sha if incremental_enabled() else "",
            # A fork's `.quorumignore` would let the branch under review decide
            # what does not get reviewed.
            trust_head_config=not from_fork,
        )

        report = report_mod.RunReport(
            models=models,
            scanning_models=scanning,
            verification_on=verify_wanted,
            trimmed_files=trimmed,
            dropped_files=dropped,
            skipped_files=skipped_files,
            incremental=ctx.incremental,
            since_sha=ctx.base_sha if ctx.incremental else "",
            head_sha=ctx.head_sha,
        )

        # -- independent scans ------------------------------------------------
        scan_budgets = workspace_mod.build(
            len(scanning), workspace_mod.MAX_CALLS, ctx.exclude_patterns
        )
        root = next((w.root for w in scan_budgets if w is not None), None)
        if root is not None and not workspace_mod.checkout_has_commit(root, ctx.head_sha):
            # The checkout is not this pull request — an issue_comment run gets
            # the default branch. Reading it would answer questions about the
            # wrong tree, so the tools are withdrawn and the summary says so.
            scan_budgets = [None] * len(scanning)
            report.repo_access = "off: the checkout does not contain this pull request"

        scans, failures = await scan_all(provider, scanning, ctx, skill, scan_budgets)
        report.scan_failures = failures

        used = [w for w in scan_budgets if w is not None]
        if used:
            report.repo_access = "on"
            report.tool_calls = sum(w.calls for w in used)
            report.files_read = list(
                dict.fromkeys(path for w in used for path in w.files_read)
            )
        elif not report.repo_access:
            report.repo_access = (
                "off by configuration"
                if not workspace_mod.access_enabled()
                else "off: no checkout available"
            )

        if not scans:
            raise ProviderUnavailable(f"every scanning model failed: {failures}")

        findings = dedupe(ledger_mod.assign_ids(consensus.merge(scans)))
        report.scanned = len(findings)
        report.per_model_counts = {
            model: len(scan) for model, scan in zip(scanning, scans, strict=False)
        }

        # Anything previously open that this scan no longer reports is treated
        # as fixed. Scans are not perfectly repeatable, so a finding can drop
        # out because the models missed it rather than because it was fixed —
        # accepted for now, since re-detection later simply reopens it.
        #
        # Only files this run actually looked at are eligible. On an
        # incremental review the diff covers just the new commits, so a finding
        # in an untouched file was never examined — closing it would report a
        # fix that nobody made.
        reviewed = set(ctx.changed_files)
        newly_closed: list[ledger_mod.LedgerEntry] = []
        for entry in list(ledger.entries.values()):
            if entry.status != "open" or entry.file_path not in reviewed:
                continue
            if not ledger.still_present(entry, findings):
                ledger.mark_fixed(entry.finding_id, ctx.head_sha)
                report.resolved.append(f"{entry.title} (`{entry.file_path}`)")
                newly_closed.append(entry)

        # Never re-report something already posted or dismissed. Matching is
        # positional, not by ID: the same model re-quotes the same bug
        # differently between runs, so ID equality alone lets duplicates
        # through — which is exactly what happened the first time this ran.
        fresh = [f for f in findings if not ledger.is_suppressed(f)]
        report.suppressed = len(findings) - len(fresh)

        # -- consensus, then verify only what is unresolved --------------------
        agreed, unresolved = consensus.split(fresh)
        report.agreed = list(agreed)

        skipped: list[Finding] = []
        if verify_wanted and unresolved:
            ranked = by_severity(unresolved)
            to_verify = ranked[:MAX_VERIFIED_FINDINGS]
            skipped = ranked[MAX_VERIFIED_FINDINGS:]

            verify_budgets = (
                workspace_mod.build(
                    len(to_verify), VERIFY_TOOL_CALLS, ctx.exclude_patterns
                )
                if any(w is not None for w in scan_budgets)
                else [None] * len(to_verify)
            )
            verified, verifier_error = await verify_all(
                provider, to_verify, models, ctx, verify_budgets
            )
            report.tool_calls += sum(w.calls for w in verify_budgets if w is not None)
            if verifier_error:
                report.verifier_error = verifier_error

            # Over the cap: never silently dropped, just demoted to advisory.
            for finding in skipped:
                finding.verdict = "uncertain"
                finding.verifier_reason = (
                    f"not verified: only the top {MAX_VERIFIED_FINDINGS} unresolved "
                    f"findings by severity are checked"
                )
        else:
            verified = unresolved

        for finding in agreed:
            report.confirmed.append(finding)

        for finding in verified + skipped:
            if not verify_wanted:
                report.unverified.append(finding)
            elif finding.verdict == "confirmed":
                report.confirmed.append(finding)
            elif finding.verdict == "refuted":
                report.refuted.append(finding)
            else:
                report.advisory.append(finding)

        # -- write back --------------------------------------------------------
        report.usage = dict(provider.usage)
        report.elapsed_seconds = time.monotonic() - started

        if dry_run:
            print(report_mod.render(report))
            return _finish(report)

        threshold = SEVERITY_RANK[inline_min_severity()]
        for finding in report.confirmed + report.unverified:
            entry = ledger_mod.LedgerEntry.from_finding(finding, ctx.head_sha)

            # Below the threshold the finding is recorded and listed in the
            # summary, but does not get its own comment in the diff view.
            if SEVERITY_RANK.get(finding.severity, 99) > threshold:
                report.summary_only.append(finding)
                ledger.record(entry)
                continue

            comment_id = await post_finding(github, number, ctx.head_sha, finding)
            if comment_id is None:
                report.unanchored.append(finding)
            entry.review_comment_id = comment_id
            ledger.record(entry)

        # Unanchored findings are shown in the summary instead, so drop them
        # from the tables to avoid listing the same thing twice.
        unanchored_ids = {f.finding_id for f in report.unanchored}
        report.confirmed = [
            f for f in report.confirmed if f.finding_id not in unanchored_ids
        ]
        report.unverified = [
            f for f in report.unverified if f.finding_id not in unanchored_ids
        ]

        report.threads_not_collapsible = await close_threads(
            github, number, newly_closed, ctx.head_sha
        )

        # Refuted findings are still recorded: keeping them stops the next run
        # from re-proposing something already dismissed.
        for finding in report.refuted + report.advisory:
            ledger.record(ledger_mod.LedgerEntry.from_finding(finding, ctx.head_sha))

        ledger.last_reviewed_sha = ctx.head_sha
        report.usage = dict(provider.usage)
        report.elapsed_seconds = time.monotonic() - started
        body = report_mod.render(report)
        marker = ledger_mod.fit_to_comment(ledger, body)
        await github.upsert_sticky_comment(number, f"{body}\n\n{marker}", sticky)
        actions.write_job_summary(body)

    return _finish(report)


def _finish(report: report_mod.RunReport) -> int:
    """Publish the run to the workflow, and decide whether to fail the check.

    Kept out of ``run`` so the exit code has one origin. A reviewer that can
    only comment is something people read when they remember to; a reviewer
    that can fail a required check is part of the process.
    """
    actions.annotate(report)
    actions.write_outputs(report)

    message = actions.gate_message(report)
    if message:
        print(f"::error title=quorum-review::{message}")
        print(f"error: {message}", file=sys.stderr)
    return actions.exit_code(report)


def list_models() -> int:
    provider = build_provider("vertex")
    for name in provider.list_models():  # type: ignore[attr-defined]
        print(name)
    return 0


def _use_utf8_output() -> None:
    """Make stdout and stderr UTF-8 regardless of the console's default.

    Findings quote source code and can be written in any language, and the
    summary uses typographic punctuation. On a console with a legacy codepage
    — cp932 on a Japanese Windows install, for instance — printing that raises
    UnicodeEncodeError and takes down a review that had already succeeded.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _use_utf8_output()
    parser = argparse.ArgumentParser(prog="quorum-review")
    parser.add_argument("--skill", default=os.getenv("REVIEW_SKILL", "security-review"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the summary instead of posting to GitHub",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="list the Vertex models this project can call, then exit",
    )
    args = parser.parse_args(argv)

    if args.list_models:
        return list_models()

    try:
        return asyncio.run(
            asyncio.wait_for(run(args.skill, args.dry_run), timeout=TIMEOUT_SECONDS)
        )
    except TimeoutError:
        print(f"review timed out after {TIMEOUT_SECONDS}s", file=sys.stderr)
        return 1
    except (GitHubError, ProviderUnavailable, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
