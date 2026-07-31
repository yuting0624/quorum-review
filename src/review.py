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

from . import consensus
from . import ledger as ledger_mod
from . import report as report_mod
from .github_client import GitHubClient, GitHubError, pr_number_from_event, read_event
from .providers import ProviderUnavailable, build_provider
from .providers.base import ReviewProvider
from .schema import SEVERITY_RANK, Finding, PRContext, Skill

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


def scan_with_all_models() -> bool:
    """Whether every configured model scans, or only the first.

    Scanning with both is what raises recall, and it costs one extra call
    regardless of diff size — but it does mean paying for two models on every
    pull request. ``QUORUM_SCAN=single`` is the cheap tier.
    """
    return os.getenv("QUORUM_SCAN", "both").strip().lower() not in {"single", "one", "1"}


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
    provider: ReviewProvider, models: list[str], ctx: PRContext, skill: Skill
) -> tuple[list[list[Finding]], list[str]]:
    """Run every model over the diff concurrently and independently.

    A model that fails does not sink the review: its results are simply absent,
    and the summary names it so nobody mistakes a degraded run for a clean one.
    """
    async def one(model: str) -> list[Finding]:
        return await provider.scan(model, ctx, skill)

    results = await asyncio.gather(
        *(one(model) for model in models), return_exceptions=True
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
    provider: ReviewProvider, findings: list[Finding], models: list[str], ctx: PRContext
) -> tuple[list[Finding], str]:
    """Judge each unresolved finding with a model that did not report it.

    Returns the findings with verdicts attached, and an error string that is
    non-empty when a verifier could not run at all. A per-finding failure is
    absorbed as ``uncertain`` — one bad call should not sink the review.
    """
    semaphore = asyncio.Semaphore(VERIFY_CONCURRENCY)
    unavailable: list[str] = []

    async def one(finding: Finding) -> Finding:
        reviewer = consensus.reviewer_for(finding, models)
        if reviewer is None:
            finding.verdict = "confirmed"
            finding.verifier_reason = "no other model was available to check this"
            return finding

        async with semaphore:
            try:
                verdict = await provider.verify(reviewer, finding, ctx)
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

    verified = await asyncio.gather(*(one(finding) for finding in findings))
    return list(verified), unavailable[0] if unavailable else ""


async def run(skill_name: str, dry_run: bool) -> int:
    event = read_event()
    number = pr_number_from_event(event)
    skill = load_skill(skill_name)

    provider = build_provider()
    models = list(provider.models)
    scanning = models if scan_with_all_models() else models[:1]
    verify_wanted = verification_enabled() and len(models) > 1

    async with GitHubClient() as github:
        ledger, sticky = await github.load_ledger(number)
        ctx, trimmed = await github.load_context(number)

        report = report_mod.RunReport(
            models=models,
            scanning_models=scanning,
            verification_on=verify_wanted,
            trimmed_files=trimmed,
            head_sha=ctx.head_sha,
        )

        # -- independent scans ------------------------------------------------
        scans, failures = await scan_all(provider, scanning, ctx, skill)
        report.scan_failures = failures
        if not scans:
            raise ProviderUnavailable(f"every scanning model failed: {failures}")

        findings = dedupe(ledger_mod.assign_ids(consensus.merge(scans)))
        report.scanned = len(findings)
        report.per_model_counts = {
            model: len(scan) for model, scan in zip(scanning, scans, strict=False)
        }

        current_ids = {finding.finding_id for finding in findings}

        # Anything previously open that this scan no longer reports is treated
        # as fixed. Scans are not perfectly repeatable, so a finding can drop
        # out because the models missed it rather than because it was fixed —
        # accepted for now, since re-detection later simply reopens it.
        for entry in list(ledger.entries.values()):
            if entry.status == "open" and entry.finding_id not in current_ids:
                ledger.mark_fixed(entry.finding_id, ctx.head_sha)
                report.resolved.append(f"{entry.title} (`{entry.file_path}`)")

        # Never re-report something already posted or dismissed.
        fresh = [f for f in findings if not ledger.is_suppressed(f.finding_id)]
        report.suppressed = len(findings) - len(fresh)

        # -- consensus, then verify only what is unresolved --------------------
        agreed, unresolved = consensus.split(fresh)
        report.agreed = list(agreed)

        skipped: list[Finding] = []
        if verify_wanted and unresolved:
            ranked = by_severity(unresolved)
            to_verify = ranked[:MAX_VERIFIED_FINDINGS]
            skipped = ranked[MAX_VERIFIED_FINDINGS:]

            verified, verifier_error = await verify_all(provider, to_verify, models, ctx)
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
        if dry_run:
            print(report_mod.render(report))
            return 0

        for finding in report.confirmed + report.unverified:
            comment_id = await github.post_inline_comment(
                number=number,
                commit_sha=ctx.head_sha,
                path=finding.file_path,
                line=finding.line,
                body=report_mod.render_inline(finding),
            )
            if comment_id is None:
                report.unanchored.append(finding)
            entry = ledger_mod.LedgerEntry.from_finding(finding, ctx.head_sha)
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

        # Refuted findings are still recorded: keeping them stops the next run
        # from re-proposing something already dismissed.
        for finding in report.refuted + report.advisory:
            ledger.record(ledger_mod.LedgerEntry.from_finding(finding, ctx.head_sha))

        ledger.last_reviewed_sha = ctx.head_sha
        body = report_mod.render(report)
        marker = ledger_mod.fit_to_comment(ledger, body)
        await github.upsert_sticky_comment(number, f"{body}\n\n{marker}", sticky)

    return 0


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
