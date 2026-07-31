"""What the reviewer must not repeat back.

Two failure directions, and they pull against each other. Under-redacting makes
the reviewer the thing that republishes a credential into a comment that
outlives the diff. Over-redacting mangles ordinary code, and a reviewer whose
comments are full of `[redacted]` where the code said `import` is a reviewer
people switch off. The second half of this file is as important as the first.
"""

from __future__ import annotations

import pytest

from quorum_review import redaction
from quorum_review.schema import Finding

REAL_LOOKING = {
    "aws access key": "AKIAIOSFODNN7EXAMPLE",
    "github token": "ghp_" + "a1B2c3D4e5F6g7H8i9J0kLmNoPqRsTuVwXyZ",
    "anthropic key": "sk-ant-api03-" + "x" * 40,
    "google api key": "AIza" + "Sy" + "A" * 33,
    "slack token": "xoxb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx",
    "stripe key": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
}


@pytest.mark.parametrize("kind,secret", sorted(REAL_LOOKING.items()))
def test_credential_shapes_are_removed(kind: str, secret: str):
    text, found = redaction.redact(f'API_KEY = "{secret}"  # committed by mistake')
    assert secret not in text
    assert redaction.PLACEHOLDER in text
    assert kind in found


def test_a_private_key_block_goes_whole():
    body = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAx0Y7Y\nnotarealkey\n"
        "-----END RSA PRIVATE KEY-----"
    )
    text, found = redaction.redact(f"the file contains:\n{body}\nwhich is committed")
    assert "MIIEowIBAAKCAQEAx0Y7Y" not in text
    assert "notarealkey" not in text
    assert found == ["private key"]


def test_a_connection_string_keeps_everything_but_the_password():
    """Knowing which database and which user is most of the finding."""
    text, found = redaction.redact("DSN = postgres://app_writer:hunter2@db.internal/app")
    assert "hunter2" not in text
    assert "app_writer" in text
    assert "db.internal/app" in text
    assert found == ["connection string password"]


def test_an_authorization_header_keeps_its_name():
    header = 'headers = {"Authorization": "Bearer abcdefghij0123456789"}'
    text, _ = redaction.redact(header)
    assert "abcdefghij0123456789" not in text
    assert "Authorization" in text


def test_the_surrounding_prose_survives():
    """A finding nobody can read is not an improvement on a finding that leaks."""
    text, _ = redaction.redact(
        f"`Config.SECRET_KEY` falls back to `{REAL_LOOKING['aws access key']}`, "
        f"which means every deployment without the variable set shares one key."
    )
    assert "Config.SECRET_KEY" in text
    assert "every deployment without the variable set shares one key" in text


# -- the other direction ---------------------------------------------------


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "def has_scope(user, action):",
        "SECRET_KEY = os.getenv('QUORUM_DEMO_SECRET', 'dev-secret-change-me')",
        "hash = sha256(path + snippet).hexdigest()[:16]",
        "url = 'https://api.github.com/repos/owner/repo/pulls/1'",
        "assert response.status_code == 200",
        "# TODO: rotate the key before release",
        "commit = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0'",
        "uuid = '550e8400-e29b-41d4-a716-446655440000'",
        "path = 'benchmark/seeded-bugs/app/permissions.py'",
    ],
)
def test_ordinary_code_is_left_alone(code: str):
    text, found = redaction.redact(code)
    assert text == code
    assert found == []


def test_a_base64_blob_is_not_assumed_to_be_a_secret():
    """The ledger marker is base64, and it appears in this project's own diffs."""
    marker = (
        "<!-- quorum-state: z:"
        "H4sIAAAAAAAAA6tWKkotLsnMS1eyUvIvyixJzUsvSizJzM9TsgIA -->"
    )
    text, found = redaction.redact(marker)
    assert text == marker
    assert found == []


# -- sanitising a whole finding --------------------------------------------


def secretive() -> Finding:
    return Finding(
        file_path="app/config.py",
        line=14,
        category="security",
        severity="critical",
        title=f"Hardcoded AWS key {REAL_LOOKING['aws access key']}",
        body=f"`{REAL_LOOKING['aws access key']}` is committed in plain text.",
        code_snippet=f'AWS_KEY = "{REAL_LOOKING["aws access key"]}"',
    )


def test_every_text_field_is_cleaned():
    finding = secretive()
    found = redaction.sanitise(finding)

    assert found
    for text in (finding.title, finding.body, finding.code_snippet):
        assert REAL_LOOKING["aws access key"] not in text


def test_the_verifier_reason_is_cleaned_too():
    """The verifier explains itself by quoting the offending value back."""
    finding = Finding(
        file_path="a.py",
        line=1,
        category="security",
        severity="high",
        title="hardcoded key",
        body="see below",
        code_snippet="",
        verifier_reason=f"confirmed: the literal {REAL_LOOKING['github token']} is real",
    )
    redaction.sanitise(finding)
    assert REAL_LOOKING["github token"] not in finding.verifier_reason


def test_a_suggestion_containing_a_secret_is_dropped_not_redacted():
    """A suggestion is applied verbatim by a click.

    Redacting it would write the placeholder into the file, which is worse than
    the leak and much harder to notice.
    """
    finding = secretive()
    finding.fix_replacement = f'AWS_KEY = "{REAL_LOOKING["aws access key"]}"  # fixme'
    finding.fix_end_line = 16

    redaction.sanitise(finding)

    assert finding.fix_replacement == ""
    assert finding.fix_end_line == 0


def test_a_clean_suggestion_survives():
    finding = secretive()
    finding.fix_replacement = 'AWS_KEY = os.environ["AWS_KEY"]'
    finding.fix_end_line = 14

    redaction.sanitise(finding)

    assert finding.fix_replacement == 'AWS_KEY = os.environ["AWS_KEY"]'


def test_sanitising_twice_changes_nothing_the_second_time():
    """It runs again after verification, so it has to be idempotent."""
    finding = secretive()
    redaction.sanitise(finding)
    before = (finding.title, finding.body, finding.code_snippet)

    assert redaction.sanitise(finding) == []
    assert (finding.title, finding.body, finding.code_snippet) == before


def test_a_clean_finding_is_untouched():
    finding = Finding(
        file_path="a.py",
        line=1,
        category="correctness",
        severity="low",
        title="mutable default argument",
        body="`scopes: list = []` is shared across calls.",
        code_snippet="def share(doc, scopes: list = []):",
    )
    original = (finding.title, finding.body, finding.code_snippet)
    assert redaction.sanitise(finding) == []
    assert (finding.title, finding.body, finding.code_snippet) == original


def test_the_note_names_what_went_without_naming_the_value():
    note = redaction.note(["aws access key", "aws access key", "private key"])
    assert "aws access key" in note
    assert "private key" in note
    assert note.count("aws access key") == 1  # deduplicated
    assert "rotate it" in note


def test_no_note_when_nothing_was_removed():
    assert redaction.note([]) == ""


def test_the_comment_says_something_was_removed():
    """A finding quoting `[redacted]` with no explanation reads like a bug."""
    from quorum_review.report import render_inline

    finding = secretive()
    finding.redacted = redaction.sanitise(finding)
    rendered = render_inline(finding)

    assert REAL_LOOKING["aws access key"] not in rendered
    assert "was removed from this comment" in rendered
    assert "rotate it" in rendered


def test_a_clean_finding_gets_no_note():
    from quorum_review.report import render_inline

    finding = Finding(
        file_path="a.py",
        line=1,
        category="correctness",
        severity="low",
        title="mutable default",
        body="shared across calls",
        code_snippet="def f(x=[]):",
    )
    assert "was removed" not in render_inline(finding)
