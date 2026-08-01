"""Merging findings from independent scans.

The reviewer asks two models to read the same diff without showing either one
the other's output, then merges the results. That merge is what fixes the
recall ceiling: with a single scanning model, anything it misses is invisible to
the rest of the pipeline, because verification only ever judges findings that
were already reported.

The merge also decides what still needs checking:

- Reported by **both** models, independently — that is the consensus. Two models
  that could not influence each other reached the same conclusion, which is
  better evidence than either one's self-reported confidence. No verification
  call is spent on it.
- Reported by **one** model — unresolved. The other model verifies it.

That is cheaper than verifying everything, not just more accurate: on a diff
where the models largely agree, only the disagreements cost a second call.
"""

from __future__ import annotations

from .matching import Report, same_defect
from .schema import SEVERITY_RANK, Finding


def as_report(finding: Finding) -> Report:
    return Report(finding.file_path, finding.line, finding.code_snippet, finding.title)


def looks_like_same(a: Finding, b: Finding) -> bool:
    """Whether two findings from different models describe the same defect."""
    return same_defect(as_report(a), as_report(b))


def _absorb(target: Finding, other: Finding) -> None:
    """Fold ``other`` into ``target``, keeping the more alarming reading.

    Severity is taken as the worst of the two rather than averaged: if one model
    thinks this is critical, that is the claim a human needs to see first, and
    the summary shows both models' names so the disagreement stays visible.
    """
    for model in other.reported_by:
        if model not in target.reported_by:
            target.reported_by.append(model)

    if SEVERITY_RANK.get(other.severity, 99) < SEVERITY_RANK.get(target.severity, 99):
        target.severity = other.severity

    # Which explanation to keep, when two models wrote one for the same defect.
    #
    # Length, and it is the weakest rule in this file. Severity takes the worse
    # of two because "worse" is defined; `reported_by` takes the union because
    # both are true. There is no comparable ordering on prose, so this stands in
    # for one — and longer is not better, it is only more.
    #
    # It is kept because the alternatives are worse rather than because it is
    # good. Asking a model which explanation is better is a third call per
    # merged finding, on the stage that exists to avoid per-finding calls, and
    # it would be answered by a model that wrote one of them. Keeping the first
    # is arbitrary in a way that depends on scan order. Keeping both doubles
    # every agreed finding in the comment a human reads.
    #
    # What bounds the damage is that this only runs when both models already
    # agree the defect is real — the disagreement, which is the interesting
    # case, never reaches here. And both names are on the comment, so a reader
    # who finds the explanation thin can see there was another.
    if len(other.body) > len(target.body):
        target.title, target.body = other.title, other.body


def merge(scans: list[list[Finding]]) -> list[Finding]:
    """Combine per-model finding lists into one deduplicated list.

    Order is preserved from the first scan onward, so the output is stable for
    a given set of inputs.
    """
    merged: list[Finding] = []

    for findings in scans:
        for finding in findings:
            match = next((m for m in merged if looks_like_same(m, finding)), None)
            if match is None:
                merged.append(finding)
            else:
                _absorb(match, finding)

    return merged


def split(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Partition into (already agreed, still needs a second opinion)."""
    agreed = [f for f in findings if f.agreed]
    unresolved = [f for f in findings if not f.agreed]
    return agreed, unresolved


def reviewer_for(finding: Finding, models: list[str]) -> str | None:
    """Pick a model to verify a finding: any model that did not report it.

    Returns None when every available model already reported it — in which case
    there is nothing left to ask and the finding is already agreed.
    """
    return next((model for model in models if model not in finding.reported_by), None)
