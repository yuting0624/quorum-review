"""The manifest is what GitHub turns into an App, so its shape gets checked.

A wrong permission here is not a test failure — it is an App with more access
than it needs, created on someone's account and rarely revisited.
"""

import json
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from create_app import _html_escape, read_manifest  # noqa: E402


@pytest.fixture(scope="module")
def manifest():
    return read_manifest(REPO_ROOT / "app-manifest.yml")


def test_it_parses(manifest):
    assert manifest["name"]
    assert manifest["url"].startswith("https://")
    assert manifest["description"]


def test_permissions_are_the_minimum_needed(manifest):
    """Exactly what the reviewer uses, and nothing else."""
    assert manifest["default_permissions"] == {
        "metadata": "read",
        "contents": "read",
        "pull_requests": "write",
    }


def test_it_cannot_write_to_the_repository(manifest):
    """Nothing here pushes a commit.

    Granting `contents: write` would raise the cost of a successful prompt
    injection from a wrong comment to a write against the repository.
    """
    assert manifest["default_permissions"].get("contents") != "write"
    assert "workflows" not in manifest["default_permissions"]
    assert "administration" not in manifest["default_permissions"]


def test_it_subscribes_to_nothing(manifest):
    """Events arrive through Actions, so the App has no reason to listen."""
    assert manifest["default_events"] == []
    assert manifest["hook_attributes"]["active"] is False


def test_it_is_private_by_default(manifest):
    """An App on a personal account should not be installable by strangers."""
    assert manifest["public"] is False


def test_the_payload_is_json_serialisable(manifest):
    """It is submitted as a form value, so it has to survive both hops."""
    escaped = _html_escape(json.dumps(manifest))
    assert '"' not in escaped
    assert "<" not in escaped


def test_a_manifest_missing_required_keys_is_rejected(tmp_path):
    bad = tmp_path / "app-manifest.yml"
    bad.write_text("description: no name and no url\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        read_manifest(bad)
