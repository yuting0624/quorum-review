"""Running against GitHub Enterprise Server, and behind a corporate proxy.

Both are the same kind of bug: the code was written against github.com, works
there, and fails on the network an enterprise actually has. Neither shows up in
this repository's own CI, so both are asserted here instead.
"""

from __future__ import annotations

import pytest

from quorum_review.github_client import ca_bundle, graphql_url

# -- GraphQL is not under the REST root on Enterprise Server ---------------


def test_dot_com_puts_graphql_under_the_api_host():
    assert graphql_url("https://api.github.com") == "https://api.github.com/graphql"


def test_enterprise_server_puts_it_beside_v3_not_under_it():
    """`GITHUB_API_URL` on GHES is `https://host/api/v3`, and GraphQL is at
    `https://host/api/graphql`. Posting to a relative `/graphql` reached
    `/api/v3/graphql`, which is a 404 on every installation."""
    assert (
        graphql_url("https://ghe.example.com/api/v3")
        == "https://ghe.example.com/api/graphql"
    )


def test_a_trailing_slash_does_not_double_up():
    assert (
        graphql_url("https://ghe.example.com/api/v3/")
        == "https://ghe.example.com/api/graphql"
    )


def test_a_host_under_a_subpath_keeps_it():
    """Some installations sit behind a path prefix."""
    assert (
        graphql_url("https://example.com/github/api/v3")
        == "https://example.com/github/api/graphql"
    )


def test_it_reads_the_environment_when_not_given_a_root(monkeypatch):
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    assert graphql_url() == "https://ghe.example.com/api/graphql"


def test_the_failure_this_prevents_is_silent(monkeypatch):
    """Thread resolution is the only GraphQL caller and it already fails
    softly, so the symptom was threads that never collapse — a feature that
    looks unimplemented rather than broken. Worth stating in a test, because
    nothing else about the run would have gone red."""
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.com/api/v3")
    assert "/api/v3/graphql" not in graphql_url()


# -- a CA bundle httpx would not otherwise read -----------------------------


@pytest.fixture(autouse=True)
def no_inherited_bundle(monkeypatch):
    for name in (
        "QUORUM_CA_BUNDLE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
    ):
        monkeypatch.delenv(name, raising=False)


def test_nothing_set_means_the_defaults(monkeypatch):
    assert ca_bundle() == ""


def test_a_bundle_that_exists_is_used(monkeypatch, tmp_path):
    bundle = tmp_path / "corp.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(bundle))

    assert ca_bundle() == str(bundle)


def test_a_stale_path_is_ignored_rather_than_fatal(monkeypatch, tmp_path):
    """A path left over from an earlier image is common. Failing closed on it
    would break a setup that works today for no gain."""
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "gone.pem"))
    assert ca_bundle() == ""


def test_the_projects_own_variable_wins(monkeypatch, tmp_path):
    ours, theirs = tmp_path / "ours.pem", tmp_path / "theirs.pem"
    ours.write_text("x")
    theirs.write_text("y")
    monkeypatch.setenv("QUORUM_CA_BUNDLE", str(ours))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(theirs))

    assert ca_bundle() == str(ours)


def test_it_falls_through_a_stale_entry_to_a_real_one(monkeypatch, tmp_path):
    real = tmp_path / "real.pem"
    real.write_text("x")
    monkeypatch.setenv("QUORUM_CA_BUNDLE", str(tmp_path / "gone.pem"))
    monkeypatch.setenv("SSL_CERT_FILE", str(real))

    assert ca_bundle() == str(real)


def test_a_directory_is_not_a_bundle(monkeypatch, tmp_path):
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(tmp_path))
    assert ca_bundle() == ""
