"""Findings as SARIF, so they land somewhere an organisation already looks.

A pull request comment is read by whoever is looking at that pull request, once.
It is not a queue, it has no owner, and nothing counts it. Every process an
organisation has built around code findings — triage rotation, exception
tracking, "no open criticals before release" — runs on something else.

SARIF is what GitHub code scanning ingests. Upload it and the findings appear in
the Security tab, deduplicated across runs, tracked open-to-fixed, filterable by
severity and rule, and available to the same reports as everything else. The
comments remain the thing people read; this is the thing people count.

Three decisions worth naming:

**The upload is the whole open state, not this run's new findings.** Code
scanning treats each upload for a category as a replacement: anything absent
from it is marked fixed. So uploading only what a run newly reported would close
every earlier alert the moment an incremental re-review found nothing new —
which is most re-reviews, because the ledger's whole job is to stop re-reporting
what it already reported. The findings therefore come from the ledger's open
entries, which is the reviewer's actual current position.

**Advisory and refuted findings are not uploaded.** The Security tab is a queue
someone is expected to empty. Filling it with findings the reviewer itself
declined to stand behind is how a queue stops being emptied. They stay in the
summary comment, where a reader can weigh them.

**The rule ID is the category, not the finding.** SARIF groups by rule, and one
rule per finding would produce a rule list as long as the findings list and no
grouping at all. Five categories give a Security tab where "we have a lot of
access-control findings this quarter" is legible.
"""

from __future__ import annotations

import json
from typing import Any

from .schema import Finding

SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
VERSION = "2.1.0"

#: GitHub renders `error` and `warning` prominently and `note` quietly, so the
#: mapping decides what a Security tab looks like at a glance. Medium maps to
#: warning rather than error: an organisation that treats every medium as a
#: blocking error stops reading the tab within a month.
LEVELS = {
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
}

#: Also carried as a numeric score, which is what GitHub sorts on.
SECURITY_SEVERITY = {
    "critical": "9.0",
    "high": "7.0",
    "medium": "5.0",
    "low": "3.0",
}

RULES: dict[str, tuple[str, str]] = {
    "security": (
        "Security defect",
        "A change that lets someone do something they should not be able to do, "
        "or exposes data that should not be exposed.",
    ),
    "correctness": (
        "Correctness defect",
        "A change that does not do what it appears to do, for inputs or "
        "sequences that can actually occur.",
    ),
    "reliability": (
        "Reliability defect",
        "A change that works until load, latency, or partial failure makes it "
        "not work.",
    ),
    "performance": (
        "Performance defect",
        "A change whose cost grows with something that grows.",
    ),
    "maintainability": (
        "Maintainability defect",
        "A change that will be misread or misused by the next person to touch it.",
    ),
}


def _rule(category: str) -> dict[str, Any]:
    name, description = RULES.get(
        category, ("Review finding", "A defect reported by cross-model review.")
    )
    return {
        "id": category,
        "name": name.replace(" ", ""),
        "shortDescription": {"text": name},
        "fullDescription": {"text": description},
        "help": {
            "text": description,
            "markdown": (
                f"**{name}** — {description}\n\n"
                "Reported by [quorum-review]"
                "(https://github.com/yuting0624/quorum-review): two models read "
                "the diff independently, and anything only one of them reported "
                "was judged by the other."
            ),
        },
        "properties": {"tags": ["quorum-review", category]},
    }


def _result(finding: Finding, models: list[str]) -> dict[str, Any]:
    # The evidence goes in the message rather than a property bag: it is the
    # thing that distinguishes this from a single-model reviewer, and a
    # property nobody renders is a property nobody reads.
    if finding.agreed:
        evidence = (
            f"Reported independently by {' and '.join(finding.reported_by)}, "
            f"neither having seen the other's output."
        )
    elif finding.verifier_model:
        reporter = finding.reported_by[0] if finding.reported_by else "one model"
        evidence = (
            f"Reported by {reporter} and confirmed by {finding.verifier_model}, "
            f"which was shown the code but not the reasoning behind the claim."
        )
    else:
        reporter = finding.reported_by[0] if finding.reported_by else "one model"
        evidence = f"Reported by {reporter}; not independently checked."

    result: dict[str, Any] = {
        "ruleId": finding.category,
        "level": LEVELS.get(finding.severity, "warning"),
        "message": {"text": f"{finding.title}\n\n{finding.body}\n\n{evidence}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.file_path},
                    # A line of 0 is not valid SARIF and GitHub rejects the
                    # whole upload for it, which would lose every other finding
                    # in the file over one bad anchor.
                    "region": {"startLine": max(1, finding.line)},
                }
            }
        ],
        # Content-addressed, so the same defect reported at a slightly
        # different line on the next run is recognised as the same alert
        # rather than opened again. This is the same identity the pull request
        # ledger uses, and for the same reason.
        "partialFingerprints": {"quorumFindingId": finding.finding_id},
        "properties": {
            "severity": finding.severity,
            "security-severity": SECURITY_SEVERITY.get(finding.severity, "5.0"),
            "reportedBy": list(finding.reported_by),
            "agreed": finding.agreed,
            "models": models,
        },
    }
    if finding.verifier_model:
        result["properties"]["verifiedBy"] = finding.verifier_model
    return result


def build(
    findings: list[Finding], models: list[str], commit: str = ""
) -> dict[str, Any]:
    """A SARIF log for one review.

    Only findings the reviewer is standing behind. See the module docstring:
    the Security tab is a queue, and a queue full of maybes is not emptied.
    """
    categories = sorted({finding.category for finding in findings}) or ["security"]
    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "quorum-review",
                "informationUri": "https://github.com/yuting0624/quorum-review",
                "rules": [_rule(category) for category in categories],
            }
        },
        "results": [_result(finding, models) for finding in findings],
    }
    if commit:
        run["versionControlProvenance"] = [{"revisionId": commit}]
    return {"$schema": SCHEMA, "version": VERSION, "runs": [run]}


def open_findings(ledger: Any) -> list[Finding]:
    """The reviewer's current position, as findings, from the ledger.

    Not this run's output. See the module docstring: an upload replaces the
    previous state, so a run that reported nothing new would otherwise close
    every alert it had raised before.

    ``wontfix`` entries are excluded along with ``fixed`` ones. Someone
    dismissed them with a reason on the pull request, and re-raising them in a
    second system is how a dismissal stops meaning anything.
    """
    return [
        Finding(
            file_path=entry.file_path,
            line=entry.line,
            category=entry.category,
            severity=entry.severity,
            title=entry.title,
            body=entry.verifier_reason or "",
            code_snippet=entry.snippet,
            finding_id=entry.finding_id,
            reported_by=list(entry.reported_by),
            verifier_model=entry.verifier_model,
        )
        for entry in ledger.entries.values()
        if entry.status == "open"
    ]


def write(
    path: str, findings: list[Finding], models: list[str], commit: str = ""
) -> None:
    import pathlib

    pathlib.Path(path).write_text(
        json.dumps(build(findings, models, commit), indent=2), encoding="utf-8"
    )
