"""Create your own GitHub App for quorum-review, from app-manifest.yml.

A single shared App cannot be distributed: everyone who installed it would need
its private key. So each adopter creates their own, and GitHub's app-manifest
flow makes that one browser round trip instead of a page of form fields.

The flow needs somewhere for GitHub to redirect back to with a one-time code,
which normally means running a web service. This runs one on localhost for the
thirty seconds it takes, then shuts it down. Nothing is hosted, and no
credential leaves your machine except the manifest itself, which contains no
secrets.

    python scripts/create_app.py

At the end you get an App ID and a private key, and instructions for storing
them as repository secrets.
"""

from __future__ import annotations

import argparse
import http.server
import json
import pathlib
import secrets
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MANIFEST_PATH = REPO_ROOT / "app-manifest.yml"

#: Only the keys GitHub's manifest flow accepts. Parsed with a tiny reader
#: rather than a YAML dependency — this script should run on a bare Python.
_SCALARS = ("name", "url", "description", "public")


def submit_form(target: str, state: str, manifest: dict[str, Any]) -> str:
    """The page that POSTs the manifest to GitHub.

    GitHub's manifest flow takes the manifest as a form POST, so the browser is
    handed a page that submits one rather than a URL to follow.

    Served from the local HTTP server rather than written to a file and opened
    with ``file://``. That was the first version and it failed: a ``file://``
    document is an opaque origin, and the POST arrives at GitHub with
    ``Origin: null`` and, depending on the browser, without the form body —
    which GitHub reports as ``"url" wasn't supplied``, an error about the
    manifest's contents for a problem with how it travelled. Serving the same
    HTML over ``http://127.0.0.1`` makes it an ordinary same-origin document
    and the POST an ordinary cross-site form submission, which is what the flow
    expects.
    """
    payload = _html_escape(json.dumps(manifest))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Creating your GitHub App</title></head>"
        "<body style='font:16px system-ui;padding:3rem'>"
        "<form method='post' action='" + _html_escape(f"{target}?state={state}") + "'>"
        f"<input type='hidden' name='manifest' value='{payload}'>"
        "<p>Opening GitHub…</p>"
        "<button type='submit'>Continue to GitHub</button>"
        "</form>"
        "<script>document.forms[0].submit()</script>"
        "</body></html>"
    )


class _Callback(http.server.BaseHTTPRequestHandler):
    """Serves the submit page, then receives GitHub's redirect with the code."""

    code: str | None = None
    state: str = ""
    page: str = ""

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        # The first visit has no query string: that is the browser asking for
        # the page that submits the manifest. GitHub's redirect back carries
        # `code` and `state`.
        if not params:
            self._respond(200, type(self).page, raw=True)
            return

        received_state = (params.get("state") or [""])[0]
        code = (params.get("code") or [""])[0]

        # The state parameter is what stops another page in your browser from
        # driving this callback with a code of its choosing.
        if not code or received_state != type(self).state:
            self._respond(400, "Something went wrong. Close this and try again.")
            return

        type(self).code = code
        self._respond(200, "App created. Close this tab and return to the terminal.")

    def _respond(self, status: int, message: str, raw: bool = False) -> None:
        body = (
            message
            if raw
            else "<html><body style='font:16px system-ui;padding:3rem'>"
            f"{message}</body></html>"
        )
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, *_args: Any) -> None:
        """Silence the default request logging."""


def read_manifest(path: pathlib.Path) -> dict[str, Any]:
    """Read the subset of YAML the manifest uses.

    A real parser would be better, and would also mean a dependency for a
    script whose whole point is to be runnable before anything is installed.
    The file is ours and its shape is fixed, so a reader for that shape is
    enough — and it fails loudly rather than guessing.
    """
    manifest: dict[str, Any] = {
        "default_events": [],
        "hook_attributes": {"active": False},
        "default_permissions": {},
    }
    section = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip() if not raw.strip().startswith("#") else ""
        if not line.strip():
            continue

        if not line.startswith(" "):
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            section = key
            if key in _SCALARS:
                manifest[key] = _scalar(value)
            elif key == "default_events" and value in ("[]", ""):
                manifest[key] = []
        elif section in ("default_permissions", "hook_attributes"):
            key, _, value = line.strip().partition(":")
            manifest[section][key.strip()] = _scalar(value.strip())

    # A multi-line `description: >-` block is common enough to be worth
    # handling rather than silently dropping.
    if manifest.get("description") in ("", ">-", ">", "|"):
        manifest["description"] = _folded_block(path, "description")

    missing = [key for key in ("name", "url") if not manifest.get(key)]
    if missing:
        raise SystemExit(f"{path.name} is missing: {', '.join(missing)}")
    return manifest


def _scalar(value: str) -> Any:
    if value in ("true", "false"):
        return value == "true"
    return value.strip("'\"")


def _folded_block(path: pathlib.Path, key: str) -> str:
    """Collect an indented block that follows `key: >-`."""
    lines = path.read_text(encoding="utf-8").splitlines()
    collected: list[str] = []
    capturing = False
    for line in lines:
        if line.startswith(f"{key}:"):
            capturing = True
            continue
        if capturing:
            stripped = line.strip()
            if line.startswith("  ") and stripped and not stripped.startswith("#"):
                collected.append(stripped)
            elif collected:
                break
    return " ".join(collected)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def exchange(code: str) -> dict[str, Any]:
    """Trade the one-time code for the App's credentials."""
    request = urllib.request.Request(
        f"https://api.github.com/app-manifests/{code}/conversions",
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "quorum-review-setup",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise SystemExit(
            f"GitHub rejected the exchange ({error.code}): {error.read().decode()[:300]}"
        ) from error


def main() -> int:
    parser = argparse.ArgumentParser(prog="create_app")
    parser.add_argument(
        "--org",
        default="",
        help="create the App under an organisation instead of your account",
    )
    parser.add_argument(
        "--out",
        default="quorum-review-app.private-key.pem",
        help="where to write the private key",
    )
    parser.add_argument(
        "--print-manifest",
        action="store_true",
        help="print the manifest and exit, to create the App by hand",
    )
    args = parser.parse_args()

    manifest = read_manifest(MANIFEST_PATH)

    target = (
        f"https://github.com/organizations/{args.org}/settings/apps/new"
        if args.org
        else "https://github.com/settings/apps/new"
    )

    if args.print_manifest:
        # For creating the App by hand, and for telling a manifest GitHub
        # rejected apart from one that never arrived.
        print(json.dumps(manifest, indent=2))
        print(f"\nPaste it at {target} (Settings → Developer settings → New GitHub App).")
        return 0

    port = free_port()
    manifest["redirect_url"] = f"http://127.0.0.1:{port}/"

    state = secrets.token_urlsafe(16)
    _Callback.state = state
    _Callback.code = None
    _Callback.page = submit_form(target, state, manifest)

    server = http.server.HTTPServer(("127.0.0.1", port), _Callback)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    entry = f"http://127.0.0.1:{port}/"
    print(f"Creating an App named {manifest['name']!r}.")
    print("A browser tab will open; confirm the App there.")
    print(f"If it does not, open {entry} yourself.\n")
    webbrowser.open(entry)

    try:
        _wait_for_code(server)
    finally:
        server.shutdown()

    if not _Callback.code:
        print("No response from GitHub — nothing was created.", file=sys.stderr)
        print(
            "If GitHub complained that a field 'wasn't supplied', the manifest "
            "did not reach it. Run with --print-manifest and create the App by "
            "hand; it is the same App either way.",
            file=sys.stderr,
        )
        return 1

    app = exchange(_Callback.code)
    key_path = pathlib.Path(args.out)
    key_path.write_text(app["pem"], encoding="utf-8")

    print(f"\nCreated: {app['html_url']}")
    print(f"Private key written to {key_path}")
    print("\nNext:")
    print("  1. Install it on your repositories:")
    print(f"       {app['html_url']}/installations/new")
    print("  2. Store the credentials as repository secrets:")
    print(f"       gh secret set APP_ID --body '{app['id']}'")
    print(f"       gh secret set APP_PRIVATE_KEY < {key_path}")
    print(f"  3. Delete the key file once stored: rm {key_path}")
    print("\nThen switch the workflow to examples/review-vertex-app.yml, which")
    print("mints a token from the App instead of using GITHUB_TOKEN.")
    return 0


def _wait_for_code(server: http.server.HTTPServer, timeout: float = 300.0) -> None:
    import time

    deadline = time.monotonic() + timeout
    while _Callback.code is None and time.monotonic() < deadline:
        time.sleep(0.2)


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


if __name__ == "__main__":
    raise SystemExit(main())
