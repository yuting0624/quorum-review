"""The ceiling a platform team wants before enabling this on two hundred repos.

The summary reports what a review cost after the fact, which answers the wrong
question. "What is the worst it can cost" is the one that gets asked, and "it
depends on the diff" is not an answer anyone can take to a budget holder.
"""

from __future__ import annotations

import pytest

from quorum_review import budget
from quorum_review.schema import ModelUsage


def usage(**models: tuple[int, int, int]) -> dict[str, ModelUsage]:
    """`model=(input, output, cached)` for each."""
    return {
        name: ModelUsage(calls=1, input_tokens=i, output_tokens=o, cached_input_tokens=c)
        for name, (i, o, c) in models.items()
    }


@pytest.fixture(autouse=True)
def no_ceiling(monkeypatch):
    monkeypatch.delenv("QUORUM_MAX_TOKENS", raising=False)


# -- the default -----------------------------------------------------------


def test_there_is_no_ceiling_by_default():
    """One that arrived switched on would truncate reviews on large pull
    requests, and the first anyone would know is a summary saying so."""
    assert budget.limit() == 0
    assert not budget.exhausted(usage(a=(10**9, 10**9, 0)), reserve=10**9)


def test_no_ceiling_means_nothing_in_the_summary():
    assert budget.note(usage(a=(100, 100, 0))) == ""


@pytest.mark.parametrize("value", ["", "  ", "lots", "-5"])
def test_an_unusable_setting_is_treated_as_no_ceiling(monkeypatch, value: str):
    """A typo must not silently start truncating reviews."""
    monkeypatch.setenv("QUORUM_MAX_TOKENS", value)
    assert budget.limit() == 0


# -- counting --------------------------------------------------------------


def test_spend_is_summed_across_models():
    assert budget.spent(usage(a=(100, 50, 0), b=(200, 25, 0))) == 375


def test_cached_input_counts():
    """It is cheaper, not free. A budget that treats it as free is one a long
    cached prompt walks straight through."""
    assert budget.spent(usage(a=(100, 0, 900))) == 1000


def test_nothing_spent_yet():
    assert budget.spent({}) == 0


# -- the ceiling binding ---------------------------------------------------


def test_room_left_means_not_exhausted(monkeypatch):
    monkeypatch.setenv("QUORUM_MAX_TOKENS", "10000")
    assert not budget.exhausted(usage(a=(1000, 500, 0)), reserve=1000)


def test_the_reserve_is_what_makes_the_ceiling_hold(monkeypatch):
    """Checking against zero would discover the overrun afterwards."""
    monkeypatch.setenv("QUORUM_MAX_TOKENS", "10000")
    spent_so_far = usage(a=(9000, 0, 0))

    assert not budget.exhausted(spent_so_far, reserve=0)
    assert budget.exhausted(spent_so_far, reserve=5000)


def test_exactly_at_the_ceiling_is_allowed(monkeypatch):
    monkeypatch.setenv("QUORUM_MAX_TOKENS", "1000")
    assert not budget.exhausted(usage(a=(600, 400, 0)), reserve=0)


def test_remaining_counts_down(monkeypatch):
    monkeypatch.setenv("QUORUM_MAX_TOKENS", "1000")
    assert budget.remaining(usage(a=(600, 100, 0))) == 300


def test_remaining_never_goes_negative(monkeypatch):
    monkeypatch.setenv("QUORUM_MAX_TOKENS", "100")
    assert budget.remaining(usage(a=(900, 900, 0))) == 0


def test_remaining_is_effectively_infinite_without_a_ceiling():
    assert budget.remaining(usage(a=(10**6, 10**6, 0))) > 10**12


def test_the_summary_shows_spend_against_the_ceiling(monkeypatch):
    monkeypatch.setenv("QUORUM_MAX_TOKENS", "50000")
    assert budget.note(usage(a=(10_000, 2_000, 0))) == "12,000 of 50,000 tokens"


# -- where it binds in a real review ---------------------------------------


def test_verification_stops_at_the_ceiling_and_says_so(monkeypatch):
    """Findings are demoted to advisory, never dropped.

    Reporting less because you ran out of money is acceptable. Reporting
    nothing, or reporting silently, is not.
    """
    import asyncio

    from quorum_review import review
    from quorum_review.schema import Finding, Verdict

    monkeypatch.setenv("QUORUM_MAX_TOKENS", "1000")
    monkeypatch.setattr(review, "VERIFY_TOKEN_RESERVE", 500)

    class SpendyProvider:
        models = ["model-a", "model-b"]

        def __init__(self):
            self.usage = {"model-b": ModelUsage()}
            self.verified: list[str] = []

        async def verify(self, model, finding, ctx, toolbox=None):
            self.verified.append(finding.finding_id)
            # Each call spends most of the ceiling.
            self.usage["model-b"].add(input_tokens=400, output_tokens=100)
            return Verdict("confirmed", "traced it", "high", model)

    def finding(fid):
        return Finding(
            file_path="a.py",
            line=1,
            category="security",
            severity="high",
            title="t",
            body="b",
            code_snippet="s",
            finding_id=fid,
            reported_by=["model-a"],
        )

    provider = SpendyProvider()
    verified, _ = asyncio.run(
        review.verify_all(
            provider,
            [finding("a"), finding("b"), finding("c"), finding("d")],
            provider.models,
            ctx=None,
        )
    )

    assert len(provider.verified) < 4, "the ceiling did not bind"
    unchecked = [f for f in verified if f.finding_id not in provider.verified]
    assert unchecked, "nothing was left over to check the message on"
    for f in unchecked:
        assert f.verdict == "uncertain"
        assert "token ceiling" in f.verifier_reason
        assert "max-tokens" in f.verifier_reason


def test_without_a_ceiling_everything_is_verified(monkeypatch):
    import asyncio

    from quorum_review import review
    from quorum_review.schema import Finding, Verdict

    monkeypatch.delenv("QUORUM_MAX_TOKENS", raising=False)

    class Provider:
        models = ["model-a", "model-b"]
        usage = {"model-b": ModelUsage(calls=1, input_tokens=10**7)}

        def __init__(self):
            self.verified: list[str] = []

        async def verify(self, model, finding, ctx, toolbox=None):
            self.verified.append(finding.finding_id)
            return Verdict("confirmed", "ok", "high", model)

    provider = Provider()
    findings = [
        Finding(
            file_path="a.py",
            line=1,
            category="security",
            severity="high",
            title="t",
            body="b",
            code_snippet="s",
            finding_id=fid,
            reported_by=["model-a"],
        )
        for fid in ("a", "b", "c")
    ]
    asyncio.run(review.verify_all(provider, findings, provider.models, ctx=None))
    assert provider.verified == ["a", "b", "c"]
