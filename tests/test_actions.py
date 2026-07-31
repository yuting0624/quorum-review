"""The exit code is the part of this that can block someone's merge.

Everything else the reviewer does is advisory — a comment you can scroll past.
The moment it becomes a required check, a wrong exit code either lets a real
finding through or stops a team shipping, so the rules are pinned here.
"""

from __future__ import annotations

import pathlib

import pytest

from quorum_review import actions
from quorum_review.report import RunReport
from quorum_review.schema import Finding


def finding(severity="high", path="a.py", title="something is wrong"):
    return Finding(
        file_path=path,
        line=1,
        category="security",
        severity=severity,
        title=title,
        body="because of reasons",
        code_snippet="bad = 1",
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "QUORUM_FAIL_ON",
        "QUORUM_FAIL_ON_DEGRADED",
        "GITHUB_OUTPUT",
        "GITHUB_STEP_SUMMARY",
    ):
        monkeypatch.delenv(name, raising=False)


# -- gating ----------------------------------------------------------------


def test_a_run_that_was_not_asked_to_gate_never_fails():
    """Failing a build that never opted in is a worse surprise than any finding."""
    report = RunReport(confirmed=[finding("critical")])
    assert actions.exit_code(report) == 0
    assert actions.gate_message(report) == ""


def test_a_finding_at_the_threshold_fails(monkeypatch):
    monkeypatch.setenv("QUORUM_FAIL_ON", "high")
    report = RunReport(confirmed=[finding("high")])
    assert actions.exit_code(report) == 1
    assert "high" in actions.gate_message(report)


def test_a_finding_above_the_threshold_fails(monkeypatch):
    monkeypatch.setenv("QUORUM_FAIL_ON", "high")
    assert actions.exit_code(RunReport(confirmed=[finding("critical")])) == 1


def test_a_finding_below_the_threshold_passes(monkeypatch):
    monkeypatch.setenv("QUORUM_FAIL_ON", "high")
    assert actions.exit_code(RunReport(confirmed=[finding("medium")])) == 0


def test_advisory_and_refuted_findings_never_gate(monkeypatch):
    """The reviewer declined to stand behind these; blocking on them teaches
    people that the verdicts mean nothing."""
    monkeypatch.setenv("QUORUM_FAIL_ON", "low")
    report = RunReport(
        advisory=[finding("critical")],
        refuted=[finding("critical")],
    )
    assert actions.exit_code(report) == 0


def test_unverified_findings_do_gate(monkeypatch):
    """With verification off, unchecked findings are what the reader is shown."""
    monkeypatch.setenv("QUORUM_FAIL_ON", "high")
    report = RunReport(verification_on=False, unverified=[finding("high")])
    assert actions.exit_code(report) == 1


def test_an_unrecognised_threshold_does_not_gate(monkeypatch):
    """A typo in a workflow must not silently start blocking merges."""
    monkeypatch.setenv("QUORUM_FAIL_ON", "sev1")
    assert actions.fail_on() == "never"
    assert actions.exit_code(RunReport(confirmed=[finding("critical")])) == 0


# -- degradation -----------------------------------------------------------


def test_a_degraded_run_passes_by_default(monkeypatch):
    """Blocking every merge because one Vertex region is unwell is its own outage."""
    monkeypatch.setenv("QUORUM_FAIL_ON", "critical")
    report = RunReport(scan_failures=["model-a: 503"])
    assert actions.exit_code(report) == 0


def test_a_degraded_run_fails_when_asked(monkeypatch):
    """A required check that passes because the reviewer was broken is worse than none."""
    monkeypatch.setenv("QUORUM_FAIL_ON_DEGRADED", "true")
    report = RunReport(scan_failures=["model-a: 503"])
    assert actions.exit_code(report) == 1
    assert "scanning model(s) failed" in actions.gate_message(report)


def test_degradation_does_not_need_a_severity_gate(monkeypatch):
    """The two policies are independent, so one must work without the other."""
    monkeypatch.setenv("QUORUM_FAIL_ON_DEGRADED", "1")
    assert actions.fail_on() == "never"
    assert actions.exit_code(RunReport(verifier_error="not entitled")) == 1


@pytest.mark.parametrize(
    "report",
    [
        RunReport(scan_failures=["model-a: 503"]),
        RunReport(verifier_error="not entitled"),
        RunReport(trimmed_files=["big.py"]),
        RunReport(dropped_files=["huge.py"]),
    ],
)
def test_every_way_of_seeing_less_counts_as_degraded(report):
    assert actions.degraded(report)


def test_a_complete_run_is_not_degraded():
    assert not actions.degraded(RunReport(confirmed=[finding()]))


# -- outputs ---------------------------------------------------------------


def test_outputs_are_written_for_later_steps(monkeypatch, tmp_path: pathlib.Path):
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    actions.write_outputs(
        RunReport(
            confirmed=[finding("critical"), finding("low")],
            advisory=[finding("high")],
            repo_access="on",
        )
    )

    written = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines()
    )
    assert written["findings"] == "2"
    assert written["critical"] == "1"
    assert written["high"] == "0"  # the advisory one is not posted as real
    assert written["low"] == "1"
    assert written["advisory"] == "1"
    assert written["degraded"] == "false"
    assert written["repo-access"] == "on"


def test_outputs_are_appended_not_overwritten(monkeypatch, tmp_path: pathlib.Path):
    """GITHUB_OUTPUT is shared with every other step in the job."""
    out = tmp_path / "out.txt"
    out.write_text("earlier-step=kept\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    actions.write_outputs(RunReport())
    assert "earlier-step=kept" in out.read_text(encoding="utf-8")


def test_nothing_is_written_outside_actions(monkeypatch):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    actions.write_outputs(RunReport())  # must not raise


def test_the_job_summary_is_written(monkeypatch, tmp_path: pathlib.Path):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    actions.write_job_summary("## Review\n\nbody")
    assert "## Review" in summary.read_text(encoding="utf-8")


def test_a_summary_that_cannot_be_written_does_not_end_the_run(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "no" / "such" / "dir.md"))
    actions.write_job_summary("body")  # must not raise
