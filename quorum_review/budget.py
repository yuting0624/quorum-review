"""A ceiling on what one review may spend.

The summary reports what a review cost after the fact, which answers the wrong
question. The one a platform team asks before enabling something on two hundred
repositories is *what is the worst it can cost*, and "it depends on the diff"
is not an answer they can take to anyone.

In tokens, not money — the same reason the summary reports tokens. Prices differ
by model, by platform, and by contract, so a currency figure computed here would
be a guess wearing the costume of a fact. A team that knows its own rate can
convert; this code cannot.

**Where it binds, and where it cannot.** A scan is a single call whose size is
decided by the diff, so the budget cannot interrupt one — the diff caps
(`max-diff-characters`, the per-file limit) are what bound that. What it does
bound is the part that scales with *findings* rather than with the diff:
verification is one call per finding, up to twenty, each with its own tool
budget. That is the difference between a predictable cost and an open one, and
it is the part a runaway review actually runs away with.

So the ceiling is checked before each verification, and a review that reaches it
stops verifying and says so. It never discards a finding: an unverified one is
demoted to advisory, which is what already happens to findings over the
`max-verified-findings` cap. Reporting less because you ran out of money is
acceptable; reporting nothing, or reporting silently, is not.
"""

from __future__ import annotations

import os

from .schema import ModelUsage


def limit() -> int:
    """Tokens one review may spend, or 0 for no ceiling.

    Unlimited by default. A ceiling that arrives switched on would silently
    truncate reviews on repositories with large pull requests, and the first
    anyone would know is a summary saying the verification stopped early.
    """
    raw = os.getenv("QUORUM_MAX_TOKENS", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, value)


def spent(usage: dict[str, ModelUsage]) -> int:
    """Tokens consumed so far, across every model.

    Cached input is counted. It is cheaper, not free, and a budget that treats
    it as free is one that a long cached prompt can walk straight through.
    """
    return sum(
        used.input_tokens + used.output_tokens + used.cached_input_tokens
        for used in usage.values()
    )


def remaining(usage: dict[str, ModelUsage]) -> int:
    """How much is left, or a large number when there is no ceiling."""
    ceiling = limit()
    if not ceiling:
        return 1 << 62
    return max(0, ceiling - spent(usage))


def exhausted(usage: dict[str, ModelUsage], reserve: int = 0) -> bool:
    """Whether the next call of roughly ``reserve`` tokens would go over.

    ``reserve`` is the caller's estimate of what one more call costs. Checking
    against it rather than against zero is the difference between a budget that
    holds and one that is discovered to have been exceeded afterwards.
    """
    ceiling = limit()
    if not ceiling:
        return False
    return spent(usage) + reserve > ceiling


def note(usage: dict[str, ModelUsage]) -> str:
    """A line for the summary when a ceiling is configured, else ""."""
    ceiling = limit()
    if not ceiling:
        return ""
    used = spent(usage)
    return f"{used:,} of {ceiling:,} tokens"
