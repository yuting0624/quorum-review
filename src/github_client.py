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
from .pathfilter import PathFilter
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


@dataclass
class ReviewThread:
    """A review thread, identified the way GraphQL needs to resolve it."""

    thread_id: str
    is_resolved: bool


def read_event() -> dict[str, Any]:
    """Load the workflow event payload written by the Actions runner.

    Absent when the pull request is named explicitly, so that a manual run does
    not need a synthesised payload.
    """
    path = os.getenv("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        if os.getenv("QUORUM_PR_NUMBER", "").strip():
            return {}
        raise GitHubError(
            "GITHUB_EVENT_PATH is not set; run inside Actions, "
            "or set QUORUM_PR_NUMBER to review a specific pull request"
        )
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def pr_number_from_event(event: dict[str, Any]) -> int:
    """Find the PR number for a pull_request or comment event.

    ``QUORUM_PR_NUMBER`` overrides the event, which is what makes
    ``workflow_dispatch`` and local re-runs possible: those carry no pull
    request in their payload.
    """
    override = os.getenv("QUORUM_PR_NUMBER", "").strip()
    if override:
        try:
            return int(override)
        except ValueError:
            raise GitHubError(f"QUORUM_PR_NUMBER is not a number: {override!r}") from None

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

    async def _pull_diff(self, number: int) -> str:
        return (
            await self._get(
                f"/repos/{self.owner}/{self.repo}/pulls/{number}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
        ).text

    async def _range_diff(self, base_sha: str, head_sha: str) -> str | None:
        """Diff between two commits, or None if the range is not resolvable.

        A force-push can make the previously reviewed commit unreachable. That
        is expected rather than exceptional, so it returns None and the caller
        falls back to the whole pull request.
        """
        try:
            response = await self._get(
                f"/repos/{self.owner}/{self.repo}/compare/{base_sha}...{head_sha}",
                headers={"Accept": "application/vnd.github.v3.diff"},
            )
        except GitHubError:
            return None
        return response.text

    async def load_context(
        self,
        number: int,
        path_filter: PathFilter | None = None,
        since_sha: str = "",
    ) -> tuple[PRContext, list[str], list[str]]:
        """Fetch everything a provider needs.

        Returns the context, the files skipped as not worth reviewing, and the
        files that were too large to send whole. Both lists are surfaced in the
        summary — a partial review must never read as a complete one.

        With ``since_sha``, only what changed since that commit is reviewed.
        The saving grows with every push: without it, a pull request on its
        tenth commit re-reads all ten commits' worth of diff every time.
        """
        pull = (await self._get(f"/repos/{self.owner}/{self.repo}/pulls/{number}")).json()
        head_sha = pull["head"]["sha"]

        raw_diff = ""
        incremental = False
        if since_sha and since_sha != head_sha:
            ranged = await self._range_diff(since_sha, head_sha)
            if ranged is not None:
                raw_diff, incremental = ranged, True

        if not incremental:
            raw_diff = await self._pull_diff(number)

        path_filter = path_filter or PathFilter()
        selected, skipped = diffs.select(raw_diff, lambda p: not path_filter.excluded(p))
        diff, trimmed = diffs.truncate(selected)

        ctx = PRContext(
            owner=self.owner,
            repo=self.repo,
            number=number,
            head_sha=head_sha,
            base_sha=since_sha if incremental else pull["base"]["sha"],
            title=pull.get("title") or "",
            body=pull.get("body") or "",
            diff=diff,
            changed_files=sorted(diffs.split_by_file(selected)),
            incremental=incremental,
        )
        return ctx, skipped, trimmed

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
        self,
        number: int,
        commit_sha: str,
        path: str,
        line: int,
        body: str,
        start_line: int | None = None,
    ) -> int | None:
        """Anchor a comment to a line or a line range, returning its ID.

        ``start_line`` spans a range, which is what a multi-line suggestion
        needs — GitHub applies a suggestion to exactly the commented lines.

        GitHub rejects an anchor that is not part of the diff with a 422. That
        is expected often enough — the model can point at a line it read for
        context rather than one the PR touched — that it is not treated as an
        error: None comes back and the caller decides what to do instead.
        """
        payload: dict[str, Any] = {
            "commit_id": commit_sha,
            "path": path,
            "line": line,
            "side": "RIGHT",
            "body": body,
        }
        if start_line is not None and start_line < line:
            payload["start_line"] = start_line
            payload["start_side"] = "RIGHT"

        response = await self._http.post(
            f"/repos/{self.owner}/{self.repo}/pulls/{number}/comments", json=payload
        )
        if response.status_code == 422:
            return None
        if response.status_code >= 400:
            raise GitHubError(
                f"could not post an inline comment on {path}:{line} -> "
                f"{response.status_code}: {response.text[:400]}"
            )
        return int(response.json()["id"])

    # -- GraphQL -----------------------------------------------------------
    #
    # REST cannot resolve a review thread, and does not expose thread IDs at
    # all. Collapsing a thread whose finding is fixed needs GraphQL, which is
    # why this small amount of it exists rather than a second API layer.

    async def _graphql(self, query: str, **variables: Any) -> dict[str, Any]:
        response = await self._http.post(
            "/graphql", json={"query": query, "variables": variables}
        )
        if response.status_code >= 400:
            raise GitHubError(f"GraphQL -> {response.status_code}: {response.text[:400]}")

        payload = response.json()
        if payload.get("errors"):
            raise GitHubError(f"GraphQL errors: {payload['errors']}")
        return payload["data"]

    _THREADS_QUERY = """
    query($owner: String!, $repo: String!, $number: Int!, $cursor: String) {
      repository(owner: $owner, name: $repo) {
        pullRequest(number: $number) {
          reviewThreads(first: 100, after: $cursor) {
            pageInfo { hasNextPage endCursor }
            nodes {
              id
              isResolved
              comments(first: 1) { nodes { databaseId } }
            }
          }
        }
      }
    }
    """

    async def review_threads(self, number: int) -> dict[int, ReviewThread]:
        """Map the first comment's REST id to its thread.

        Keying on the first comment is what bridges the two APIs: inline
        comments are posted over REST, which returns a numeric id, while
        resolving needs the GraphQL node id of the thread that comment started.
        """
        threads: dict[int, ReviewThread] = {}
        cursor: str | None = None

        while True:
            data = await self._graphql(
                self._THREADS_QUERY,
                owner=self.owner,
                repo=self.repo,
                number=number,
                cursor=cursor,
            )
            block = data["repository"]["pullRequest"]["reviewThreads"]
            for node in block["nodes"]:
                comments = node["comments"]["nodes"]
                if not comments:
                    continue
                database_id = comments[0].get("databaseId")
                if database_id is not None:
                    threads[int(database_id)] = ReviewThread(
                        thread_id=node["id"], is_resolved=bool(node["isResolved"])
                    )

            if not block["pageInfo"]["hasNextPage"]:
                return threads
            cursor = block["pageInfo"]["endCursor"]

    _RESOLVE_MUTATION = """
    mutation($threadId: ID!) {
      resolveReviewThread(input: {threadId: $threadId}) {
        thread { id isResolved }
      }
    }
    """

    async def resolve_thread(self, thread_id: str) -> None:
        await self._graphql(self._RESOLVE_MUTATION, threadId=thread_id)

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
