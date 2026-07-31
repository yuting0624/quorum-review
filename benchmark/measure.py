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
        --primary claude-opus-5 --verifier gemini-3.6-flash
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from src import ledger as ledger_mod
from src import review as review_mod
from src.github_client import GitHubClient
from src.providers import build_provider
from src.schema import Finding, PRContext

# Answer key. Each entry is (path suffix, keywords); a finding matches when the
# path matches and any keyword appears in its title or body. Keywords are
# specific enough to separate the several bugs that share a file.
SEEDED = {
    "B1": ("app/search.py", ("sql inject", "sql-inject")),
    "B2": ("app/export.py", ("traversal",)),
    "B3": ("app/sharing.py", ("constant-time", "constant time", "timing")),
    "B4": ("app/fetcher.py", ("ssrf", "request forgery")),
    "B5": ("app/config.py", ("secret", "fallback", "hardcoded")),
    "B6": ("app/admin.py", ("delete_document", "ownership", "no admin")),
    "B7": ("app/export.py", ("toctou", "time-of-check", "time of check")),
    "B8": ("app/sharing.py", ("mutable default",)),
    "B9": ("app/documents.py", ("unbounded", "clamp", "page size", "page-size")),
    "B10": ("app/admin.py", ("swallow", "bare except", "except")),
}

# Correct code that looks dangerous. A finding matching one of these is a false
# positive — that is the entire reason they are in the fixture.
DECOYS = {
    "D1": ("app/indexer.py", ("subprocess", "command inject", "shell")),
    "D2": ("app/plugins.py", ("import",)),
    "D3": ("app/fetcher.py", ("random", "predictable", "entropy")),
}

# Real bugs that were written by accident. Reporting them is a true positive.
UNSEEDED = {
    "U1": ("app/sharing.py", ("expir", "ttl", "never expire")),
    "U2": ("app/sharing.py", ("scope",)),
}


def classify(finding: Finding) -> str | None:
    """Return the answer-key ID this finding matches, or None.

    Seeded bugs are checked before the accidental ones so that a finding which
    could read as either is credited to the answer key.
    """
    haystack = f"{finding.title} {finding.body}".lower()
    for table in (SEEDED, UNSEEDED, DECOYS):
        for key, (path, keywords) in table.items():
            if not finding.file_path.endswith(path):
                continue
            if any(word in haystack for word in keywords):
                return key
    return None


def score(findings: list[Finding]) -> dict[str, object]:
    seeded: set[str] = set()
    unseeded: set[str] = set()
    decoys: set[str] = set()
    unclassified: list[str] = []

    for finding in findings:
        key = classify(finding)
        if key is None:
            unclassified.append(f"{finding.file_path}: {finding.title}")
        elif key in SEEDED:
            seeded.add(key)
        elif key in UNSEEDED:
            unseeded.add(key)
        else:
            decoys.add(key)

    return {
        "seeded": seeded,
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
        f"unseeded {','.join(sorted(result['unseeded'])) or '-'}",  # type: ignore[arg-type]
        f"FP {','.join(sorted(result['decoys'])) or '0'}",  # type: ignore[arg-type]
    ]
    if result["unclassified"]:
        parts.append(f"unclassified {len(result['unclassified'])}")  # type: ignore[arg-type]
    return " | ".join(parts)


async def one_run(provider, ctx: PRContext, skill, verify: bool) -> tuple[list, list]:
    """Return (primary findings, findings surviving verification)."""
    findings = review_mod.dedupe(ledger_mod.assign_ids(await provider.scan(ctx, skill)))
    if not verify:
        return findings, findings

    ranked = review_mod.by_severity(findings)[: review_mod.MAX_VERIFIED_FINDINGS]
    verified, error = await review_mod.verify_all(provider, ranked, ctx)
    if error:
        print(f"    ! verifier unavailable: {error}", file=sys.stderr)
        return findings, findings
    return findings, [f for f in verified if f.verdict == "confirmed"]


async def main_async(args: argparse.Namespace) -> int:
    if args.primary:
        os.environ["PRIMARY_MODEL"] = args.primary
    if args.verifier:
        os.environ["VERIFIER_MODEL"] = args.verifier

    provider = build_provider()
    skill = review_mod.load_skill(args.skill)

    # Fetch the pull request once; every run reviews byte-identical input.
    async with GitHubClient() as github:
        ctx, trimmed = await github.load_context(args.pr)
    if trimmed:
        print(f"warning: files truncated before review: {trimmed}", file=sys.stderr)

    print(f"primary  : {provider.primary_model}")
    print(f"verifier : {provider.verifier_model if args.verify else '(disabled)'}")
    print(f"skill    : {skill.name}   pr: #{args.pr}   runs: {args.runs}\n")

    primary_hits: list[set] = []
    final_hits: list[set] = []

    for index in range(1, args.runs + 1):
        primary, survived = await one_run(provider, ctx, skill, args.verify)
        p_score, f_score = score(primary), score(survived)
        primary_hits.append(p_score["seeded"])  # type: ignore[arg-type]
        final_hits.append(f_score["seeded"])  # type: ignore[arg-type]

        print(f"run {index}")
        print(f"  primary  ({p_score['total']:2d} findings): {fmt(p_score)}")
        if args.verify:
            print(f"  verified ({f_score['total']:2d} findings): {fmt(f_score)}")
        for item in p_score["unclassified"]:  # type: ignore[union-attr]
            print(f"    ? unclassified: {item}")

    # Stability across runs is the number worth reporting: a bug found once in
    # three is not the same result as a bug found three times in three.
    print("\nper-bug hit rate across runs (primary / after verification)")
    for key in sorted(SEEDED, key=lambda k: int(k[1:])):
        p = sum(key in hits for hits in primary_hits)
        f = sum(key in hits for hits in final_hits)
        flag = "" if p == args.runs else ("  <- unstable" if p else "  <- never found")
        print(f"  {key:<4} {p}/{args.runs}  {f}/{args.runs}{flag}")

    mean_p = sum(len(h) for h in primary_hits) / args.runs
    mean_f = sum(len(h) for h in final_hits) / args.runs
    print(
        f"\nmean seeded found: primary {mean_p:.1f}/10, "
        f"after verification {mean_f:.1f}/10"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="benchmark.measure")
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--primary", default="")
    parser.add_argument("--verifier", default="")
    parser.add_argument("--skill", default="security-review")
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="primary model only, to measure the recall ceiling on its own",
    )
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
