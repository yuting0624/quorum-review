"""Asking GitHub again when GitHub says to come back later.

A review posts one comment per finding, which is the exact shape the secondary
rate limit exists to slow down: a burst of writes from one actor. Hitting it
without a retry loses the remaining findings *and still reports success* — a
partial review that reads as a complete one.

The hard part is not the backoff, it is telling a throttle from a permission
failure. GitHub returns 403 for both.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from quorum_review import github_client as gh


def response(status: int, body: str = "", **headers: str) -> httpx.Response:
    return httpx.Response(
        status, text=body, headers=headers, request=httpx.Request("GET", "https://x/")
    )


# -- telling a throttle from a refusal -------------------------------------


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_transient_statuses_are_retried(status: int):
    assert gh._worth_retrying(response(status))


def test_a_secondary_rate_limit_is_retried():
    body = "You have exceeded a secondary rate limit. Please wait a few minutes."
    assert gh._worth_retrying(response(403, body))


def test_an_exhausted_primary_limit_is_retried():
    assert gh._worth_retrying(
        response(403, "API rate limit exceeded", **{"x-ratelimit-remaining": "0"})
    )


def test_a_permission_failure_is_not_retried():
    """Same status as a throttle. Retrying it four times just delays the error."""
    body = '{"message": "Resource not accessible by integration"}'
    assert not gh._worth_retrying(
        response(403, body, **{"x-ratelimit-remaining": "4999"})
    )


@pytest.mark.parametrize("status", [200, 201, 400, 404, 422])
def test_ordinary_outcomes_are_not_retried(status: int):
    assert not gh._worth_retrying(response(status))


def test_a_422_from_an_unanchorable_comment_is_not_retried():
    """GitHub rejects a line outside the diff. Asking again gets the same answer,
    and the caller already degrades by re-posting without the suggestion."""
    assert not gh._worth_retrying(response(422, "line must be part of the diff"))


# -- how long to wait ------------------------------------------------------


def test_the_servers_own_instruction_is_used():
    assert gh._retry_after(response(403, "", **{"retry-after": "37"})) == 37.0


def test_a_reset_timestamp_is_turned_into_a_delay():
    import time

    soon = str(int(time.time()) + 30)
    delay = gh._retry_after(
        response(403, "", **{"x-ratelimit-reset": soon, "x-ratelimit-remaining": "0"})
    )
    assert 20 <= delay <= 31


def test_a_reset_in_the_past_does_not_produce_a_negative_wait():
    delay = gh._retry_after(
        response(403, "", **{"x-ratelimit-reset": "1", "x-ratelimit-remaining": "0"})
    )
    assert delay == 0.0


def test_a_reset_with_quota_remaining_is_ignored():
    """Every response carries the header; only an exhausted one means anything."""
    assert gh._retry_after(
        response(200, "", **{"x-ratelimit-reset": "99999999999",
                             "x-ratelimit-remaining": "4000"})
    ) == 0.0


def test_a_nonsense_header_falls_back_rather_than_raising():
    assert gh._retry_after(response(429, "", **{"retry-after": "soon"})) == 0.0


def test_no_headers_means_no_instruction():
    assert gh._retry_after(response(503)) == 0.0


def test_the_backoff_grows():
    delays = [gh._backoff(n) for n in range(1, 4)]
    assert delays == sorted(delays)
    assert delays[0] < delays[-1]


# -- the loop --------------------------------------------------------------


class FakeTransport:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    async def request(self, method, path, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_with(outcomes: list) -> gh.GitHubClient:
    client = gh.GitHubClient(token="t", repository="o/r")
    client._http = FakeTransport(outcomes)  # type: ignore[assignment]
    return client


@pytest.fixture
def instant_backoff(monkeypatch):
    """Scoped, not autouse: the delay functions are themselves under test above."""
    monkeypatch.setattr(gh, "_backoff", lambda attempt: 0.0)
    monkeypatch.setattr(gh, "_retry_after", lambda response: 0.0)


pytestmark_loop = pytest.mark.usefixtures("instant_backoff")


@pytestmark_loop
def test_a_throttled_request_succeeds_on_a_later_attempt():
    client = client_with([response(429), response(429), response(200, "ok")])
    result = asyncio.run(client._send("GET", "/x"))

    assert result.status_code == 200
    assert client._http.calls == 3  # type: ignore[attr-defined]


@pytestmark_loop
def test_a_request_that_works_is_made_once():
    client = client_with([response(200, "ok")])
    asyncio.run(client._send("GET", "/x"))
    assert client._http.calls == 1  # type: ignore[attr-defined]


@pytestmark_loop
def test_the_last_response_is_returned_rather_than_raised(monkeypatch):
    """The caller turns a status into a GitHubError with its own context."""
    monkeypatch.setattr(gh, "HTTP_ATTEMPTS", 2)
    client = client_with([response(429), response(429)])
    assert asyncio.run(client._send("GET", "/x")).status_code == 429
    assert client._http.calls == 2  # type: ignore[attr-defined]


@pytestmark_loop
def test_a_dropped_connection_is_retried():
    client = client_with(
        [httpx.ConnectError("reset"), response(200, "ok")]
    )
    assert asyncio.run(client._send("GET", "/x")).status_code == 200


@pytestmark_loop
def test_a_connection_that_never_comes_back_becomes_a_github_error(monkeypatch):
    monkeypatch.setattr(gh, "HTTP_ATTEMPTS", 2)
    client = client_with([httpx.ConnectError("reset"), httpx.ConnectError("reset")])
    with pytest.raises(gh.GitHubError, match="reset"):
        asyncio.run(client._send("GET", "/x"))
