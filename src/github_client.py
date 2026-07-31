"""GitHub REST wrapper.

Phase 0 needs REST only. Collapsing resolved threads requires GraphQL
(``resolveReviewThread``) and lands with incremental re-review in Phase 1.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx

from . import diffs
from .ledger import MARKER_PREFIX, Ledger, decode_marker
from .schema import PRContext

API_ROOT = os.getenv("GITHUB_API_URL", "https://api.github.com")
_TIMEOUT = httpx.Timeout(30.0, read=60.0)


class GitHubError(RuntimeError):
    pass


@dataclass
class StickyComment:
    """The single summary comment that also carries the ledger marker."""

    comment_id: int
    body: str


def read_event() -> dict[str, Any]:
    """Load the workflow event payload written by the Actions runner."""
    path = os.getenv("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        raise GitHubError("GITHUB_EVENT_PATH is not set; this must run inside Actions")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def pr_number_from_event(event: dict[str, Any]) -> int:
    """Find the PR number for either a pull_request or a comment event."""
    if "pull_request" in event and isinstance(event["pull_request"], dict):
        return int(event["pull_request"]["number"])
    issue = event.get("issue")
    if isinstance(issue, dict) and issue.get("pull_request"):
        return int(issue["number"])
    raise GitHubError("this event does not refer to a pull request")


class GitHubClient:
    def __init__(self, token: str | None = None, repository: str | None = None) -> None:
        token = token or os.getenv("GITHUB_TOKEN", "")
        if not token:
            raise GitHubError("GITHUB_TOKEN is not set")

        repository = repository or os.getenv("GITHUB_REPOSITORY", "")
        if "/" not in repository:
            raise GitHubError("GITHUB_REPOSITORY must look like 'owner/repo'")
        self.owner, self.repo = repository.split("/", 1)

        self._http = httpx.AsyncClient(
            base_url=API_ROOT,
            timeout=_TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "quorum-review",
            },
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._http.aclose()

    # -- reads -------------------------------------------------------------

    async def _get(self, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._http.get(path, **kwargs)
        if response.status_code >= 400:
            raise GitHubError(
                f"GET {path} -> {response.status_code}: {response.text[:400]}"
            )
        return response

    async def load_context(self, number: int) -> tuple[PRContext, list[str]]:
        """Fetch everything a provider needs, plus the list of trimmed files."""
        pull = (await self._get(f"/repos/{self.owner}/{self.repo}/pulls/{number}")).json()

        raw_diff = (
            await self._get(
                f"/repos/{self.owner}/{self.repo}/pulls/{number}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
        ).text

        diff, trimmed = diffs.truncate(raw_diff)

        ctx = PRContext(
            owner=self.owner,
            repo=self.repo,
            number=number,
            head_sha=pull["head"]["sha"],
            base_sha=pull["base"]["sha"],
            title=pull.get("title") or "",
            body=pull.get("body") or "",
            diff=diff,
            changed_files=sorted(diffs.split_by_file(raw_diff)),
        )
        return ctx, trimmed

    async def find_sticky_comment(self, number: int) -> StickyComment | None:
        """Locate our own summary comment by its ledger marker."""
        page = 1
        while True:
            response = await self._get(
                f"/repos/{self.owner}/{self.repo}/issues/{number}/comments",
                params={"per_page": 100, "page": page},
            )
            comments = response.json()
            if not comments:
                return None
            for comment in comments:
                body = comment.get("body") or ""
                if MARKER_PREFIX in body:
                    return StickyComment(comment_id=int(comment["id"]), body=body)
            if len(comments) < 100:
                return None
            page += 1

    async def load_ledger(self, number: int) -> tuple[Ledger, StickyComment | None]:
        sticky = await self.find_sticky_comment(number)
        if sticky is None:
            return Ledger.empty(number), None
        return decode_marker(sticky.body) or Ledger.empty(number), sticky

    # -- writes ------------------------------------------------------------

    async def upsert_sticky_comment(
        self, number: int, body: str, sticky: StickyComment | None
    ) -> int:
        """Create the summary comment, or edit the existing one in place.

        Editing rather than appending is what keeps a long-running PR from
        accumulating a column of stale bot comments.
        """
        if sticky is None:
            response = await self._http.post(
                f"/repos/{self.owner}/{self.repo}/issues/{number}/comments",
                json={"body": body},
            )
        else:
            response = await self._http.patch(
                f"/repos/{self.owner}/{self.repo}/issues/comments/{sticky.comment_id}",
                json={"body": body},
            )

        if response.status_code >= 400:
            raise GitHubError(
                f"could not write the summary comment -> "
                f"{response.status_code}: {response.text[:400]}"
            )
        return int(response.json()["id"])

    async def post_inline_comment(
        self, number: int, commit_sha: str, path: str, line: int, body: str
    ) -> int | None:
        """Anchor a comment to a line, returning its ID.

        GitHub rejects an anchor that is not part of the diff with a 422. That
        is expected often enough — the model can point at a line it read for
        context rather than one the PR touched — that it is not treated as an
        error: None comes back and the caller folds the finding into the
        summary instead of failing the run.
        """
        response = await self._http.post(
            f"/repos/{self.owner}/{self.repo}/pulls/{number}/comments",
            json={
                "commit_id": commit_sha,
                "path": path,
                "line": line,
                "side": "RIGHT",
                "body": body,
            },
        )
        if response.status_code == 422:
            return None
        if response.status_code >= 400:
            raise GitHubError(
                f"could not post an inline comment on {path}:{line} -> "
                f"{response.status_code}: {response.text[:400]}"
            )
        return int(response.json()["id"])

    async def reply_to_comment(self, number: int, comment_id: int, body: str) -> None:
        """Reply inside an existing review thread rather than starting a new one."""
        response = await self._http.post(
            f"/repos/{self.owner}/{self.repo}/pulls/{number}/comments/{comment_id}/replies",
            json={"body": body},
        )
        if response.status_code >= 400:
            raise GitHubError(
                f"could not reply to comment {comment_id} -> "
                f"{response.status_code}: {response.text[:400]}"
            )
