"""The workflow files, checked for the things GitHub rejects silently.

A workflow GitHub cannot parse does not fail loudly. It produces a run named
after the file path, with no jobs, and an email saying "No jobs were run" — and
if the workflow is one nobody in this repository calls, that is the only signal
there is. `reusable.yml` sat broken for weeks that way: it had
`uses: ${{ inputs.action-ref }}`, which is not allowed at step level, so the
org-wide rollout path this project recommends had never worked.

Nothing here replaces `actionlint`. These are the specific mistakes that have
actually been made, plus the invariants the two entry points have to share.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
FILES = sorted(WORKFLOWS.glob("*.yml"))
EXAMPLES = sorted((Path(__file__).resolve().parent.parent / "examples").glob("*.yml"))


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def steps_of(document: dict) -> list[tuple[str, dict]]:
    return [
        (name, step)
        for name, job in (document.get("jobs") or {}).items()
        for step in (job.get("steps") or [])
    ]


def test_there_are_workflows_to_check():
    """A glob that matches nothing passes every parametrised test below."""
    assert FILES


@pytest.mark.parametrize("path", FILES + EXAMPLES, ids=lambda p: p.name)
def test_it_parses(path: Path):
    assert isinstance(load(path), dict)


@pytest.mark.parametrize("path", FILES + EXAMPLES, ids=lambda p: p.name)
def test_no_expression_in_a_step_level_uses(path: Path):
    """The bug that broke `reusable.yml`. GitHub refuses to parse the file, so
    it never reads the workflow's name — which is why the failing runs were
    titled `.github/workflows/reusable.yml` rather than `quorum-review
    (reusable)`. That is the tell."""
    for job, step in steps_of(load(path)):
        assert "${{" not in str(step.get("uses", "")), (
            f"{path.name}: job {job!r} has an expression in `uses:`. "
            f"Check the action out with `ref:` and use a literal path instead."
        )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_third_party_actions_are_version_pinned(path: Path):
    for job, step in steps_of(load(path)):
        uses = str(step.get("uses", ""))
        if uses and not uses.startswith("."):
            assert "@" in uses, f"{path.name}: job {job!r} uses {uses!r} unpinned"


# -- what the two entry points have to agree on -----------------------------


ENTRY_POINTS = [WORKFLOWS / "review.yml", WORKFLOWS / "reusable.yml"]


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_the_reviewer_is_not_pointed_at_its_own_checkout(path: Path):
    """Two checkouts, and the models must read the pull request rather than the
    action. `workspace-path` is what says which is which; without it the action
    reviews itself and reports nothing about the change."""
    review = [
        step
        for _job, step in steps_of(load(path))
        if str(step.get("uses", "")).startswith("./")
    ]
    assert len(review) == 1, f"{path.name}: expected exactly one local action step"
    assert "pr-head" in str(review[0].get("with", {}).get("workspace-path", ""))


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_the_pull_request_is_checked_out_somewhere_of_its_own(path: Path):
    checkouts = [
        step
        for _job, step in steps_of(load(path))
        if str(step.get("uses", "")).startswith("actions/checkout")
        and "refs/pull/" in str(step.get("with", {}).get("ref", ""))
    ]
    assert checkouts, f"{path.name}: nothing checks out the pull request"
    for step in checkouts:
        assert step["with"].get("path") == "pr-head"
        assert step["with"].get("persist-credentials") is False


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_the_permissions_the_sarif_upload_needs_are_granted(path: Path):
    """`actions: read` is the one that is easy to miss: without it the upload
    fails on a private repository with "Resource not accessible by
    integration", which does not sound like a permissions problem."""
    permissions = load(path).get("permissions") or {}
    assert permissions.get("security-events") == "write"
    assert permissions.get("actions") == "read"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_contents_stays_read_only(path: Path):
    """Nothing here pushes a commit. Granting write raises the cost of a
    successful prompt injection from a wrong comment to a write against
    someone's repository."""
    assert (load(path).get("permissions") or {}).get("contents") == "read"


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_the_loop_guard_is_present(path: Path):
    """Without it, the comments this action posts satisfy the trigger and start
    another run."""
    condition = " ".join(str(load(path)["jobs"]["review"]["if"]).split())
    assert "comment.user.type != 'Bot'" in condition


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_forks_are_excluded_from_the_non_fork_workflows(path: Path):
    """Reviewing a fork needs `pull_request_target`, which is a different trust
    model and its own file."""
    condition = " ".join(str(load(path)["jobs"]["review"]["if"]).split())
    assert "head.repo.fork != true" in condition


# -- the reusable workflow specifically -------------------------------------

REUSABLE = WORKFLOWS / "reusable.yml"


def call_spec() -> dict:
    document = load(REUSABLE)
    return (
        document[True]["workflow_call"]
        if True in document
        else document["on"]["workflow_call"]
    )


def test_adopting_it_does_not_start_writing_to_the_security_tab():
    """SARIF needs GitHub Advanced Security, and a shared workflow should not
    begin writing to another repository's Security tab because someone bumped
    the version. Same principle as `fail-on: never`."""
    assert call_spec()["inputs"]["sarif-file"]["default"] == ""


def test_adopting_it_does_not_start_blocking_merges():
    assert call_spec()["inputs"]["fail-on"]["default"] == "never"


def test_the_action_checkout_can_be_given_a_token():
    """The default GITHUB_TOKEN is scoped to the calling repository, so it
    cannot clone the action from a private or internal one."""
    assert "ACTION_REPOSITORY_TOKEN" in call_spec()["secrets"]

    fetch = next(
        step
        for _job, step in steps_of(load(REUSABLE))
        if step.get("name") == "Fetch the reviewer"
    )
    assert "ACTION_REPOSITORY_TOKEN" in str(fetch["with"]["token"])
    assert "github.token" in str(fetch["with"]["token"])


def test_the_old_input_name_is_gone_rather_than_reinterpreted():
    """`action-ref` took `owner/repo@ref`; `action-version` takes a git ref.
    Keeping the name would mean silently reading `yuting0624/quorum-review@v1`
    as a branch, which fails later and less clearly than "invalid input"."""
    inputs = call_spec()["inputs"]
    assert "action-version" in inputs
    assert "action-ref" not in inputs


def test_the_sarif_upload_checks_there_is_something_to_upload():
    """A review that died before writing the file would otherwise send
    codeql-action after a path that does not exist, and the error it reports
    for that reads like a permissions problem."""
    upload = next(
        step
        for _job, step in steps_of(load(REUSABLE))
        if step.get("name") == "Upload SARIF"
    )
    assert "steps.sarif.outputs.found" in str(upload["if"])


# -- the App token, and its silent fallback ---------------------------------


APP_TOKEN_FILES = [
    WORKFLOWS / "review.yml",
    Path(__file__).resolve().parent.parent / "examples" / "review-vertex-app.yml",
]


@pytest.mark.parametrize("path", APP_TOKEN_FILES, ids=lambda p: p.name)
def test_a_failed_mint_is_not_silent(path: Path):
    """`continue-on-error` reports the step as successful whatever happened, so
    a mint that failed leaves a green run and comments still posted by
    github-actions. That is exactly what an App created, configured and never
    *installed* looks like, and the reason was four hundred lines into a log.

    `outcome` is the real result; `conclusion` is the one continue-on-error
    rewrites. The check has to be on `outcome`."""
    steps = dict(steps_of(load(path)))
    warn = [
        step
        for _job, step in steps_of(load(path))
        if "could not be minted" in str(step.get("name", ""))
    ]
    assert warn, f"{path.name}: a failed mint would be invisible"
    assert "steps.app-token.outcome" in str(warn[0]["if"]), (
        "must key on `outcome`; `conclusion` is rewritten by continue-on-error"
    )
    assert steps is not None


@pytest.mark.parametrize("path", APP_TOKEN_FILES, ids=lambda p: p.name)
def test_the_review_falls_back_rather_than_stopping(path: Path):
    """A fork, a revoked installation or an expired key should degrade the
    review, not end it."""
    mint = next(
        step for _job, step in steps_of(load(path)) if step.get("id") == "app-token"
    )
    assert mint.get("continue-on-error") is True

    review = next(
        step
        for _job, step in steps_of(load(path))
        if step.get("name") == "Review" or str(step.get("uses", "")).startswith("./")
    )
    token = str(review["with"]["github-token"])
    assert "steps.app-token.outputs.token" in token
    assert "github.token" in token


ALL_ENTRY_POINTS = FILES + EXAMPLES


@pytest.mark.parametrize(
    "path",
    [p for p in ALL_ENTRY_POINTS if "review" in p.name],
    ids=lambda p: p.name,
)
def test_every_workflow_that_posts_can_post(path: Path):
    """`pull-requests: write` wherever a review runs, including the App-token
    example — especially there. The App normally does the posting, so it is
    tempting to drop the permission; then the fallback runs with GITHUB_TOKEN
    and fails on the first comment. A fallback that cannot do the job is not a
    fallback."""
    document = load(path)
    if "review" not in (document.get("jobs") or {}):
        pytest.skip("not a review workflow")
    assert (document.get("permissions") or {}).get("pull-requests") == "write"
