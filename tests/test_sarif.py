"""The SARIF has to be valid enough that GitHub accepts the whole upload.

Code scanning rejects a log wholesale rather than skipping a bad result, so one
finding anchored at line 0 loses every other finding in the run. The shape
assertions here are about that: not "is this good SARIF" but "will this be
thrown away".
"""

from __future__ import annotations

import json
import pathlib

import pytest

from quorum_review import sarif
from quorum_review.schema import Finding


def finding(**kwargs) -> Finding:
    base = {
        "file_path": "app/search.py",
        "line": 18,
        "category": "security",
        "severity": "critical",
        "title": "SQL injection via f-string interpolation",
        "body": "The search term reaches the query unparameterised.",
        "code_snippet": "f\"... MATCH '{term}'\"",
        "finding_id": "abc123",
        "reported_by": ["gemini-3.6-flash", "claude-opus-5"],
    }
    base.update(kwargs)
    return Finding(**base)


@pytest.fixture
def log() -> dict:
    return sarif.build(
        [finding()],
        ["gemini-3.6-flash", "claude-opus-5"],
        "deadbeef",
        "https://github.com/o/r",
    )


# -- shape -----------------------------------------------------------------


def test_it_declares_the_version_github_expects(log: dict):
    assert log["version"] == "2.1.0"
    assert log["$schema"].endswith("sarif-2.1.0.json")


def test_it_serialises(log: dict):
    assert json.loads(json.dumps(log)) == log


def test_every_result_names_a_rule_that_exists(log: dict):
    driver = log["runs"][0]["tool"]["driver"]
    declared = {rule["id"] for rule in driver["rules"]}
    for result in log["runs"][0]["results"]:
        assert result["ruleId"] in declared


def test_the_commit_is_recorded():
    log = sarif.build([finding()], ["m"], "deadbeef", "https://github.com/o/r")
    assert log["runs"][0]["versionControlProvenance"][0]["revisionId"] == "deadbeef"


def test_a_run_with_no_findings_is_still_a_valid_log():
    """Uploading nothing is how code scanning learns the alerts are fixed."""
    log = sarif.build([], ["m"], "sha")
    assert log["runs"][0]["results"] == []
    assert log["runs"][0]["tool"]["driver"]["rules"]  # a driver needs at least one


# -- the things that get an upload rejected --------------------------------


def test_a_line_of_zero_becomes_one():
    """startLine must be >= 1. GitHub rejects the whole log otherwise, which
    would lose every other finding over one bad anchor."""
    log = sarif.build([finding(line=0)], ["m"])
    region = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
    assert region["startLine"] == 1


def test_the_path_is_repository_relative():
    log = sarif.build([finding(file_path="app/search.py")], ["m"])
    uri = log["runs"][0]["results"][0]["locations"][0]["physicalLocation"][
        "artifactLocation"
    ]["uri"]
    assert uri == "app/search.py"
    assert not uri.startswith("/")


def test_an_unknown_category_still_produces_a_rule():
    """A model can return a category outside the schema's enum."""
    log = sarif.build([finding(category="architecture")], ["m"])
    assert log["runs"][0]["tool"]["driver"]["rules"][0]["id"] == "architecture"
    assert log["runs"][0]["results"][0]["ruleId"] == "architecture"


def test_an_unknown_severity_gets_a_level_rather_than_none():
    log = sarif.build([finding(severity="catastrophic")], ["m"])
    assert log["runs"][0]["results"][0]["level"] == "warning"


# -- what the Security tab will show ---------------------------------------


@pytest.mark.parametrize(
    "severity,level",
    [("critical", "error"), ("high", "error"), ("medium", "warning"), ("low", "note")],
)
def test_severity_maps_to_a_level(severity: str, level: str):
    log = sarif.build([finding(severity=severity)], ["m"])
    assert log["runs"][0]["results"][0]["level"] == level


def test_the_fingerprint_is_the_finding_id():
    """Content-addressed, so the same defect at a shifted line is the same alert
    rather than a new one — the same identity the pull request ledger uses."""
    log = sarif.build([finding(finding_id="abc123")], ["m"])
    prints = log["runs"][0]["results"][0]["partialFingerprints"]
    assert prints["quorumFindingId"] == "abc123"


def test_independent_agreement_is_in_the_message():
    """It is the thing that distinguishes this from a single-model reviewer, and
    a property nobody renders is a property nobody reads."""
    log = sarif.build([finding()], ["m"])
    text = log["runs"][0]["results"][0]["message"]["text"]
    assert "independently" in text
    assert "gemini-3.6-flash" in text and "claude-opus-5" in text


def test_a_verified_finding_names_its_verifier():
    log = sarif.build(
        [finding(reported_by=["claude-opus-5"], verifier_model="gemini-3.6-flash")],
        ["m"],
    )
    result = log["runs"][0]["results"][0]
    assert "gemini-3.6-flash" in result["message"]["text"]
    assert result["properties"]["verifiedBy"] == "gemini-3.6-flash"


def test_an_unchecked_finding_says_so():
    log = sarif.build([finding(reported_by=["claude-opus-5"])], ["m"])
    assert "not independently checked" in log["runs"][0]["results"][0]["message"]["text"]


def test_a_security_severity_score_is_carried():
    """GitHub sorts on this rather than on `level`."""
    log = sarif.build([finding(severity="critical")], ["m"])
    assert log["runs"][0]["results"][0]["properties"]["security-severity"] == "9.0"


# -- writing ---------------------------------------------------------------


def test_write_produces_a_readable_file(tmp_path: pathlib.Path):
    path = tmp_path / "quorum.sarif"
    sarif.write(str(path), [finding()], ["m"], "sha")
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == "2.1.0"


# -- the whole open state, not this run --------------------------------------


class FakeLedger:
    def __init__(self, entries):
        self.entries = {e.finding_id: e for e in entries}


def entry(finding_id="a", status="open", **kwargs):
    from quorum_review.ledger import LedgerEntry

    base = {
        "finding_id": finding_id,
        "file_path": "app/x.py",
        "category": "security",
        "severity": "high",
        "title": "something",
        "line": 3,
        "snippet": "bad = 1",
        "reported_by": ["model-a"],
        "status": status,
    }
    base.update(kwargs)
    return LedgerEntry(**base)


def test_open_findings_come_from_the_ledger_not_the_run():
    """Code scanning treats an upload as a replacement.

    A re-review that reports nothing new — which the ledger exists to make the
    common case — would otherwise close every alert earlier reviews raised.
    """
    ledger = FakeLedger([entry("a"), entry("b"), entry("c")])
    assert {f.finding_id for f in sarif.open_findings(ledger)} == {"a", "b", "c"}


def test_fixed_findings_are_left_out():
    ledger = FakeLedger([entry("a"), entry("b", status="fixed")])
    assert [f.finding_id for f in sarif.open_findings(ledger)] == ["a"]


def test_dismissed_findings_are_left_out():
    """Re-raising a dismissal in a second system is how a dismissal stops
    meaning anything."""
    ledger = FakeLedger([entry("a", status="wontfix")])
    assert sarif.open_findings(ledger) == []


def test_an_empty_ledger_uploads_an_empty_result_set():
    """Which is how code scanning learns the alerts are resolved."""
    log = sarif.build(sarif.open_findings(FakeLedger([])), ["m"], "sha")
    assert log["runs"][0]["results"] == []


def test_the_ledger_carries_enough_to_build_a_result():
    ledger = FakeLedger([entry("a", verifier_model="model-b")])
    log = sarif.build(sarif.open_findings(ledger), ["m"], "sha")
    result = log["runs"][0]["results"][0]
    assert result["ruleId"] == "security"
    assert result["partialFingerprints"]["quorumFindingId"] == "a"
    assert result["properties"]["verifiedBy"] == "model-b"


# -- what has actually been rejected ---------------------------------------


def test_a_complete_log_passes_the_checks_that_have_bitten_us(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    log = sarif.build([finding()], ["m"], "deadbeef")
    assert sarif.problems(log) == []


def test_provenance_carries_the_repository_uri(monkeypatch):
    """Code scanning rejected an upload for exactly this, losing every finding."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.example.com")
    provenance = sarif.build([finding()], ["m"], "sha")["runs"][0][
        "versionControlProvenance"
    ][0]
    assert provenance["repositoryUri"] == "https://github.example.com/owner/repo"
    assert provenance["revisionId"] == "sha"


def test_provenance_is_omitted_rather_than_half_filled(monkeypatch):
    """A rejection loses the whole run, so an incomplete block is worse than none."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    log = sarif.build([finding()], ["m"], "sha")
    assert "versionControlProvenance" not in log["runs"][0]
    assert sarif.problems(log) == []


def test_the_checker_notices_a_missing_repository_uri():
    log = sarif.build([finding()], ["m"], "", "")
    log["runs"][0]["versionControlProvenance"] = [{"revisionId": "sha"}]
    assert any("repositoryUri" in problem for problem in sarif.problems(log))


def test_the_checker_notices_an_undeclared_rule():
    log = sarif.build([finding()], ["m"], "", "")
    log["runs"][0]["results"][0]["ruleId"] = "invented"
    assert any("not declared" in problem for problem in sarif.problems(log))
