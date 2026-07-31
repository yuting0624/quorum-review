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
    return sarif.build([finding()], ["gemini-3.6-flash", "claude-opus-5"], "deadbeef")


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
    log = sarif.build([finding()], ["m"], "deadbeef")
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
