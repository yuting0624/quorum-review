"""Which model failures are worth asking about again.

A single 429 currently loses a whole scan, and on the dual-scan design that
quietly halves the review — the summary reports one model down, which is honest
but is not something a reader should have to act on for a transient rate limit.

The classification is what matters here, more than the backoff. Retrying an
entitlement error spends the run's whole time budget before reporting what it
already knew on the first attempt.
"""

from __future__ import annotations

import asyncio

import pytest

from quorum_review.providers import vertex
from quorum_review.providers.base import ProviderUnavailable


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    """The schedule is not what is being tested, and sleeping for it is slow."""
    monkeypatch.setattr(vertex, "RETRY_BASE_DELAY", 0.0)


@pytest.mark.parametrize(
    "message",
    [
        "429 RESOURCE_EXHAUSTED: quota exceeded",
        "503 Service Unavailable",
        "The model is overloaded, please try again",
        "Deadline exceeded",
        "connection reset by peer",
    ],
)
def test_transient_failures_are_retried(message):
    assert vertex._is_retryable(RuntimeError(message))


@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailable("claude-opus-5 is not entitled: 404"),
        ProviderUnavailable("permission denied"),
        RuntimeError("400 INVALID_ARGUMENT: schema is malformed"),
        ValueError("hit max_tokens; raise the budget"),
    ],
)
def test_permanent_failures_are_not_retried(error):
    assert not vertex._is_retryable(error)


def test_an_entitlement_error_wrapped_as_unavailable_is_never_retried():
    """Ordering matters: a 503 whose body mentions 'permission' must still retry,
    and a 404 must not, so the class is checked before the text."""
    unavailable = vertex._as_unavailable("m", RuntimeError("404 not found"))
    assert isinstance(unavailable, ProviderUnavailable)
    assert not vertex._is_retryable(unavailable)


def test_a_transient_failure_succeeds_on_a_later_attempt():
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("503 Service Unavailable")
        return "ok"

    assert asyncio.run(vertex._with_retry("m", flaky)) == "ok"
    assert len(attempts) == 3


def test_retries_stop_at_the_limit(monkeypatch):
    monkeypatch.setattr(vertex, "MAX_ATTEMPTS", 2)
    attempts = []

    async def always_429():
        attempts.append(1)
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    with pytest.raises(RuntimeError, match="429"):
        asyncio.run(vertex._with_retry("m", always_429))
    assert len(attempts) == 2


def test_a_permanent_failure_is_raised_on_the_first_attempt():
    attempts = []

    async def entitlement():
        attempts.append(1)
        raise ProviderUnavailable("not entitled")

    with pytest.raises(ProviderUnavailable):
        asyncio.run(vertex._with_retry("m", entitlement))
    assert len(attempts) == 1


def test_a_call_that_works_is_not_repeated():
    attempts = []

    async def fine():
        attempts.append(1)
        return "ok"

    assert asyncio.run(vertex._with_retry("m", fine)) == "ok"
    assert len(attempts) == 1


def test_a_failure_to_reach_the_runners_own_token_endpoint_is_retried():
    """Seen in Actions, and it took down a whole run.

    Both models authenticate off the same credential, so this fails both at
    once — the summary then reports every scanning model down over something
    that clears in seconds.
    """
    error = RuntimeError(
        "('Unable to retrieve Identity Pool subject token', 'upstream connect "
        "error or disconnect/reset before headers. retried and the latest reset "
        "reason: remote connection failure, transport failure reason: delayed "
        "connect error: Connection refused')"
    )
    assert vertex._is_retryable(error)
    assert not isinstance(vertex._as_unavailable("m", error), ProviderUnavailable)
