"""The GitHub App setup script.

It failed on first real use with GitHub reporting `"url" wasn't supplied` — for
a manifest that contains a `url`. The manifest was fine; it did not arrive. The
page carrying it was written to a file and opened over `file://`, which is an
opaque origin, so the POST reached GitHub with `Origin: null` and no usable
body. GitHub described that as a problem with the manifest's contents.

The page is served over `http://127.0.0.1` now. These tests cover the reader,
the page, and the callback, because the flow itself needs a browser and a
GitHub account and so is never exercised by CI.
"""

from __future__ import annotations

import http.server
import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from create_app import (  # noqa: E402
    MANIFEST_PATH,
    _Callback,
    free_port,
    read_manifest,
    submit_form,
)

TARGET = "https://github.com/settings/apps/new"


@pytest.fixture
def manifest() -> dict:
    return read_manifest(MANIFEST_PATH)


# -- the manifest the repository ships --------------------------------------


def test_it_has_the_fields_github_requires(manifest: dict):
    """`name` and `url` are the two GitHub refuses without."""
    assert manifest["name"]
    assert manifest["url"].startswith("https://")


def test_the_description_survives_the_folded_block(manifest: dict):
    """`description: >-` spans lines, and dropping it silently would produce an
    App with no description rather than an error."""
    assert "Cross-model" in manifest["description"]
    assert "\n" not in manifest["description"]


def test_it_asks_for_no_webhook(manifest: dict):
    """Events reach the reviewer through Actions. An App that listens to
    nothing has nothing to attack."""
    assert manifest["hook_attributes"] == {"active": False}
    assert manifest["default_events"] == []


def test_it_does_not_ask_to_write_contents(manifest: dict):
    """Nothing here pushes a commit. Granting it would raise the cost of a
    successful prompt injection from a wrong comment to a write."""
    assert manifest["default_permissions"]["contents"] == "read"


def test_it_can_resolve_a_thread(manifest: dict):
    """The one thing the default GITHUB_TOKEN cannot do, and the reason this
    App exists at all."""
    assert manifest["default_permissions"]["pull_requests"] == "write"


def test_a_manifest_missing_a_required_field_is_refused(tmp_path: Path):
    incomplete = tmp_path / "app-manifest.yml"
    incomplete.write_text("name: Something\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="url"):
        read_manifest(incomplete)


# -- the page that carries it -----------------------------------------------


def test_the_page_carries_the_whole_manifest(manifest: dict):
    page = submit_form(TARGET, "st4te", manifest)
    start = page.index("value='") + len("value='")
    field = page[start : page.index("'", start)]
    decoded = (
        field.replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&amp;", "&")
    )

    assert json.loads(decoded) == manifest


def test_the_page_posts_rather_than_gets(manifest: dict):
    assert "method='post'" in submit_form(TARGET, "st4te", manifest)


def test_the_state_travels_in_the_action(manifest: dict):
    assert "?state=st4te" in submit_form(TARGET, "st4te", manifest)


def test_there_is_a_button_for_when_the_script_does_not_run(manifest: dict):
    """The auto-submit is a convenience. A browser that blocks it should leave
    something to click rather than a blank page."""
    page = submit_form(TARGET, "st4te", manifest)
    assert "<button type='submit'>" in page


# -- the callback -----------------------------------------------------------


class Server:
    def __init__(self, state: str = "st4te"):
        self.port = free_port()
        _Callback.state = state
        _Callback.code = None
        _Callback.page = submit_form(TARGET, state, {"name": "x", "url": "https://y"})
        self._http = http.server.HTTPServer(("127.0.0.1", self.port), _Callback)
        threading.Thread(target=self._http.serve_forever, daemon=True).start()

    def get(self, path: str) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
            return response.read().decode()

    def close(self) -> None:
        self._http.shutdown()


@pytest.fixture
def server():
    running = Server()
    yield running
    running.close()


def test_the_bare_path_serves_the_form(server: Server):
    """This is the change: the page comes from the local server rather than a
    file, so the POST to GitHub is made by an ordinary http origin."""
    assert "name='manifest'" in server.get("/")


def test_the_redirect_captures_the_code(server: Server):
    server.get("/?code=abc123&state=st4te")
    assert _Callback.code == "abc123"


def test_a_wrong_state_is_refused(server: Server):
    """What stops another page in your browser from driving this callback with
    a code of its choosing."""
    with pytest.raises(urllib.error.HTTPError) as caught:
        server.get("/?code=evil&state=guessed")

    assert caught.value.code == 400
    assert _Callback.code is None


def test_a_redirect_with_no_code_is_refused(server: Server):
    with pytest.raises(urllib.error.HTTPError):
        server.get("/?state=st4te")
    assert _Callback.code is None
