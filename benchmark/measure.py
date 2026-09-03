"""Repeated measurement against the seeded-bugs pull request.

Single runs are noisy — the same model reported 12 findings on one pass and 11
on the next — so any claim about which arrangement is better needs repetition.
This drives the provider directly, posts nothing, and scores each run against
the answer key in `seeded-bugs/README.md`.

Every run yields two data points: what the primary model reported, and what
survived verification. That matters because **verification can only raise
precision, never recall** — it never sees a bug the primary failed to report.
The primary's recall is a hard ceiling on the pipeline's recall.

Usage:

    GITHUB_TOKEN=... GITHUB_REPOSITORY=owner/repo GOOGLE_CLOUD_PROJECT=... \\
      python -m benchmark.measure --pr 1 --runs 3 \\
        --primary gemini-3.8-flash --verifier claude-sonnet-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

from quorum_review import consensus
from quorum_review import ledger as ledger_mod
from quorum_review import review as review_mod
from quorum_review import workspace as workspace_mod
from quorum_review.github_client import GitHubClient
from quorum_review.providers import build_provider
from quorum_review.schema import Finding, PRContext

# Answer key. Each entry is (path suffix, keywords); a finding matches when the
# path matches and any keyword appears in its title or body. Keywords are
# specific enough to separate the several bugs that share a file.
SEEDED = {
    "B1": ("app/search.py", ("sql inject", "sql-inject")),
    # "traversal" alone once scored a hit as a miss: the finding was titled
    # "Export filename joined into a path without validation (arbitrary file
    # write)", which is the same bug named after its consequence.
    "B2": ("app/export.py", ("traversal", "arbitrary file", "basename")),
    "B3": ("app/sharing.py", ("constant-time", "constant time", "timing")),
    "B4": ("app/fetcher.py", ("ssrf", "request forgery")),
    "B5": ("app/config.py", ("secret", "fallback", "hardcoded")),
    "B6": ("app/admin.py", ("delete_document", "ownership", "no admin")),
    "B7": ("app/export.py", ("toctou", "time-of-check", "time of check")),
    "B8": ("app/sharing.py", ("mutable default",)),
    "B9": ("app/documents.py", ("unbounded", "clamp", "page size", "page-size")),
    "B10": ("app/admin.py", ("swallow", "bare except", "except")),
}

# Real bugs whose correctness is only decidable by reading a file the pull
# request does not touch. Scored separately from the seeded ten so the older
# numbers stay comparable.
CONTEXT_BUGS = {
    "C2": ("app/reports.py", ("has_scope", "required_scopes", "scope")),
    "C3": ("app/reports.py", ("audit", "argument order", "argument", "signature")),
}

# Correct code that looks dangerous. A finding matching one of these is a false
# positive — that is the entire reason they are in the fixture.
#
# C1 is the one that matters: "is this validated upstream" is the commonest
# cause of false positives, and it is exactly what a diff cannot answer.
DECOYS = {
    "D1": ("app/indexer.py", ("subprocess", "command inject", "shell")),
    "D2": ("app/plugins.py", ("import",)),
    "D3": ("app/fetcher.py", ("random", "predictable", "entropy")),
    # C4 before C1: both are path handling in reports.py, and C1's keywords are
    # broad enough to claim a C4 finding. C4's name is distinctive, so it goes
    # first — the same title-collision that once hid C3 inside C2.
    "C4": ("app/reports.py", ("write_named_report", "named report")),
    "C1": ("app/reports.py", ("write_report", "traversal", "sanitis", "sanitiz")),
}

# Real bugs that were written by accident. Reporting them is a true positive.
UNSEEDED = {
    "U1": ("app/sharing.py", ("expir", "ttl", "never expire")),
    "U2": ("app/sharing.py", ("scope",)),
    "U3": ("app/fetcher.py", ("size limit", "unbounded", "memory")),
    # Found by the models in run after run and credited to nobody, because the
    # answer key only listed what was planted. All three are real.
    "U4": ("app/plugins.py", ("do not exist", "does not exist", "registry")),
    "U5": ("app/export.py", ("document.export", "scope check", "scope")),
    "U6": ("app/admin.py", ("orphan", "all users", "globally", "every user")),
}


def classify(finding: Finding) -> str | None:
    """Return the answer-key ID this finding matches, or None.

    The title is tried on its own before the body is brought in. Three of these
    cases share one file and mention each other in passing — a report about
    `audit.record` explains which action was being logged, and the action is
    the one the scope check is about — so matching on the whole text credited
    both findings to the same ID and made the second look like a miss. What a
    finding is *about* is what its title says.

    Seeded bugs are checked before the accidental ones so that a finding which
    could read as either is credited to the answer key.
    """
    return _match(finding, finding.title.lower()) or _match(
        finding, f"{finding.title} {finding.body}".lower()
    )


def _match(finding: Finding, haystack: str) -> str | None:
    for table in (SEEDED, CONTEXT_BUGS, UNSEEDED, DECOYS):
        for key, (path, keywords) in table.items():
            if not finding.file_path.endswith(path):
                continue
            if any(word in haystack for word in keywords):
                return key
    return None


def score(findings: list[Finding]) -> dict[str, object]:
    seeded: set[str] = set()
    context: set[str] = set()
    unseeded: set[str] = set()
    decoys: set[str] = set()
    unclassified: list[str] = []

    for finding in findings:
        key = classify(finding)
        if key is None:
            unclassified.append(f"{finding.file_path}: {finding.title}")
        elif key in SEEDED:
            seeded.add(key)
        elif key in CONTEXT_BUGS:
            context.add(key)
        elif key in UNSEEDED:
            unseeded.add(key)
        else:
            decoys.add(key)

    return {
        "seeded": seeded,
        "context": context,
        "unseeded": unseeded,
        "decoys": decoys,
        "unclassified": unclassified,
        "total": len(findings),
    }


def fmt(result: dict[str, object]) -> str:
    seeded = sorted(result["seeded"], key=lambda k: int(k[1:]))  # type: ignore[index]
    missed = sorted(set(SEEDED) - set(result["seeded"]), key=lambda k: int(k[1:]))  # type: ignore[arg-type]
    parts = [
        f"{len(seeded):2d}/10 seeded",
        f"missed {','.join(missed) if missed else '-'}",
        f"context {','.join(sorted(result['context'])) or '-'}"  # type: ignore[arg-type]
        f"/{len(CONTEXT_BUGS)}",
        f"unseeded {','.join(sorted(result['unseeded'])) or '-'}",  # type: ignore[arg-type]
        f"FP {','.join(sorted(result['decoys'])) or '0'}",  # type: ignore[arg-type]
    ]
    if result["unclassified"]:
        parts.append(f"unclassified {len(result['unclassified'])}")  # type: ignore[arg-type]
    return " | ".join(parts)


async def one_run(
    provider, ctx: PRContext, skill, scanners: list[str], verify: bool
) -> tuple[list, list]:
    """Return (everything the scans reported, everything that survived).

    Both halves are reported because they answer different questions: the first
    is the recall ceiling the scanning models set, and the second is what a
    human would actually see.
    """
    # Fresh budgets every run. Reusing one would let run 3 inherit run 1's
    # spent allowance and quietly measure a weaker reviewer.
    scan_budgets = workspace_mod.build(
        len(scanners), workspace_mod.MAX_CALLS, ctx.exclude_patterns
    )
    scans, failures = await review_mod.scan_all(
        provider, scanners, ctx, skill, scan_budgets
    )
    for failure in failures:
        print(f"    ! scan failed: {failure}", file=sys.stderr)

    opened = list(
        dict.fromkeys(p for w in scan_budgets if w is not None for p in w.files_read)
    )
    if opened:
        calls = sum(w.calls for w in scan_budgets if w is not None)
        print(f"    tools: {calls} call(s), opened {', '.join(opened)}")

    findings = review_mod.dedupe(ledger_mod.assign_ids(consensus.merge(scans)))
    agreed, unresolved = consensus.split(findings)

    if not verify or not unresolved:
        return findings, findings

    ranked = review_mod.by_severity(unresolved)[: review_mod.MAX_VERIFIED_FINDINGS]
    verify_budgets = (
        workspace_mod.build(
            len(ranked), review_mod.VERIFY_TOOL_CALLS, ctx.exclude_patterns
        )
        if any(w is not None for w in scan_budgets)
        else [None] * len(ranked)
    )
    verified, error = await review_mod.verify_all(
        provider, ranked, list(provider.models), ctx, verify_budgets
    )
    if error:
        print(f"    ! verifier unavailable: {error}", file=sys.stderr)
        return findings, findings

    survived = agreed + [f for f in verified if f.verdict == "confirmed"]
    return findings, survived


async def main_async(args: argparse.Namespace) -> int:
    if args.primary:
        os.environ["PRIMARY_MODEL"] = args.primary
    if args.verifier:
        os.environ["VERIFIER_MODEL"] = args.verifier
    if args.workspace:
        os.environ["GITHUB_WORKSPACE"] = args.workspace

    provider = build_provider()
    skill = review_mod.load_skill(args.skill)
    scanners = list(provider.models) if args.scan_both else list(provider.models)[:1]

    # Fetch the pull request once; every run reviews byte-identical input.
    async with GitHubClient() as github:
        # No path filter: the fixture is the whole point, and the defaults
        # would be free to decide part of it is not worth reviewing.
        ctx, _skipped, trimmed, dropped = await github.load_context(
            args.pr, use_default_excludes=False
        )
    if trimmed:
        print(f"warning: files truncated before review: {trimmed}", file=sys.stderr)

    root = workspace_mod.workspace_root()
    access = "off (no --workspace)" if root is None else str(root)
    if root is not None and not workspace_mod.checkout_has_commit(root, ctx.head_sha):
        access += f"  <- WARNING: does not contain {ctx.head_sha[:7]}"

    print(f"models   : {', '.join(provider.models)}")
    print(f"scanning : {', '.join(scanners)}")
    print(f"verify   : {'on' if args.verify else 'off'}")
    print(f"repo     : {access}")
    print(f"skill    : {skill.name}   pr: #{args.pr}   runs: {args.runs}\n")

    scan_hits: list[set] = []
    final_hits: list[set] = []
    transcript: list[dict[str, object]] = []

    before = _usage_snapshot(provider)

    for index in range(1, args.runs + 1):
        reported, survived = await one_run(provider, ctx, skill, scanners, args.verify)
        after = _usage_snapshot(provider)
        spent, before = _usage_delta(before, after), after
        transcript.append(
            {"reported": _dump(reported), "survived": _dump(survived), "usage": spent}
        )
        p_score, f_score = score(reported), score(survived)
        scan_hits.append(p_score["seeded"])  # type: ignore[arg-type]
        final_hits.append(f_score["seeded"])  # type: ignore[arg-type]

        print(f"run {index}")
        print(f"  scanned  ({p_score['total']:2d} findings): {fmt(p_score)}")
        if args.verify:
            print(f"  survived ({f_score['total']:2d} findings): {fmt(f_score)}")
        for item in p_score["unclassified"]:  # type: ignore[union-attr]
            print(f"    ? unclassified: {item}")
        if args.show:
            for finding in reported:
                print(
                    f"    [{classify(finding) or '--'}] "
                    f"{finding.file_path}:{finding.line} {finding.title}"
                )

    # Stability across runs is the number worth reporting: a bug found once in
    # three is not the same result as a bug found three times in three.
    print("\nper-bug hit rate across runs (scanned / survived)")
    for key in sorted(SEEDED, key=lambda k: int(k[1:])):
        p = sum(key in hits for hits in scan_hits)
        f = sum(key in hits for hits in final_hits)
        flag = "" if p == args.runs else ("  <- unstable" if p else "  <- never found")
        print(f"  {key:<4} {p}/{args.runs}  {f}/{args.runs}{flag}")

    mean_p = sum(len(h) for h in scan_hits) / args.runs
    mean_f = sum(len(h) for h in final_hits) / args.runs
    print(f"\nmean seeded found: scanned {mean_p:.1f}/10, survived {mean_f:.1f}/10")

    _print_usage([run["usage"] for run in transcript], args.runs)

    if args.save:
        pathlib.Path(args.save).write_text(
            json.dumps(transcript, indent=2), encoding="utf-8"
        )
        print(f"findings written to {args.save}")
    return 0


def _usage_snapshot(provider: object) -> dict[str, dict[str, int]]:
    """Per-model counters as they stand right now.

    The provider accumulates across runs, so a per-run figure is the difference
    between two snapshots. Taken that way rather than by resetting, so the
    harness does not reach into the provider's state to measure it.
    """
    return {
        model: {
            "calls": used.calls,
            "input": used.input_tokens,
            "cached_input": used.cached_input_tokens,
            "output": used.output_tokens,
        }
        for model, used in getattr(provider, "usage", {}).items()
    }


def _usage_delta(
    before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    return {
        model: {
            field: value - before.get(model, {}).get(field, 0)
            for field, value in counts.items()
        }
        for model, counts in after.items()
    }


def _print_usage(per_run: list[dict[str, dict[str, int]]], runs: int) -> None:
    """What the configuration cost, in tokens.

    Tokens rather than money, for the reason in ``ModelUsage``: prices differ
    by model, platform and contract, so a figure computed here would be a guess
    wearing the costume of a fact. What a comparison between configurations
    needs is the quantity, and that is the part we observed.

    Recall was the only axis this harness measured, which made "two models find
    more" impossible to weigh against "one model costs less". Both halves are
    on the same page now.
    """
    totals: dict[str, dict[str, int]] = {}
    for run in per_run:
        for model, counts in run.items():
            entry = totals.setdefault(
                model, {"calls": 0, "input": 0, "cached_input": 0, "output": 0}
            )
            for field, value in counts.items():
                entry[field] += value

    if not totals:
        return

    print("\nmean tokens per run")
    print(f"  {'model':<20} {'calls':>6} {'input':>10} {'cached':>10} {'output':>9}")
    for model, counts in sorted(totals.items()):
        print(
            f"  {model:<20} {counts['calls'] / runs:6.1f} "
            f"{counts['input'] / runs:10,.0f} {counts['cached_input'] / runs:10,.0f} "
            f"{counts['output'] / runs:9,.0f}"
        )


def _dump(findings: list[Finding]) -> list[dict[str, str]]:
    return [
        {"file_path": f.file_path, "title": f.title, "body": f.body} for f in findings
    ]


def rescore(path: str, runs_label: str = "") -> int:
    """Re-apply the answer key to a saved run.

    Every mis-scored result so far has been a keyword gap rather than a model
    failure — a finding titled after the consequence instead of the mechanism,
    or two cases in one file collapsing onto the same ID. Fixing the key then
    cost a full set of paid runs to re-measure, which is a strong incentive not
    to fix it. Saving the findings removes that.
    """
    transcript = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    scan_hits, final_hits = [], []

    print(f"rescoring {path}{runs_label}\n")
    for index, entry in enumerate(transcript, start=1):
        reported = [_load(item) for item in entry["reported"]]
        survived = [_load(item) for item in entry["survived"]]
        p_score, f_score = score(reported), score(survived)
        scan_hits.append(p_score["seeded"])
        final_hits.append(f_score["seeded"])
        print(f"run {index}")
        print(f"  scanned  ({p_score['total']:2d} findings): {fmt(p_score)}")
        print(f"  survived ({f_score['total']:2d} findings): {fmt(f_score)}")
        for item in p_score["unclassified"]:  # type: ignore[union-attr]
            print(f"    ? unclassified: {item}")

    total = len(transcript)
    print("\nper-bug hit rate across runs (scanned / survived)")
    for key in sorted(SEEDED, key=lambda k: int(k[1:])):
        p = sum(key in hits for hits in scan_hits)
        f = sum(key in hits for hits in final_hits)
        flag = "" if p == total else ("  <- unstable" if p else "  <- never found")
        print(f"  {key:<4} {p}/{total}  {f}/{total}{flag}")

    mean_p = sum(len(h) for h in scan_hits) / total
    mean_f = sum(len(h) for h in final_hits) / total
    print(f"\nmean seeded found: scanned {mean_p:.1f}/10, survived {mean_f:.1f}/10")
    return 0


def _load(item: dict[str, str]) -> Finding:
    return Finding(
        file_path=item["file_path"],
        line=0,
        category="security",
        severity="medium",
        title=item["title"],
        body=item["body"],
        code_snippet="",
    )


def main() -> int:
    parser = argparse.ArgumentParser(prog="benchmark.measure")
    parser.add_argument("--pr", type=int, default=0)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--primary", default="")
    parser.add_argument("--verifier", default="")
    parser.add_argument("--skill", default="security-review")
    parser.add_argument(
        "--show",
        action="store_true",
        help="print every finding with the answer-key ID it matched",
    )
    parser.add_argument(
        "--save",
        default="",
        help="write every finding to this JSON file so it can be rescored later",
    )
    parser.add_argument(
        "--rescore",
        default="",
        help=(
            "re-apply the answer key to a saved run and exit. Correcting a "
            "keyword should not cost a paid re-measurement."
        ),
    )
    parser.add_argument(
        "--workspace",
        default="",
        help=(
            "path to a checkout of the pull request's head, which the models "
            "may read. In Actions this is GITHUB_WORKSPACE; locally, make one "
            "with `git worktree add`. Omit to measure the diff-only reviewer."
        ),
    )
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="skip the second opinion on findings only one model reported",
    )
    parser.add_argument(
        "--single-scan",
        dest="scan_both",
        action="store_false",
        help="only the first model scans, to measure one model's recall ceiling",
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    if args.rescore:
        return rescore(args.rescore)
    if not args.pr:
        parser.error("--pr is required unless --rescore is given")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
