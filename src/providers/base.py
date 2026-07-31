"""The ReviewProvider protocol.

**This abstraction is the backbone of the project** (PRD §4.3). Two things
depend on it:

1. Swapping an implementation once Managed Agents reaches GA.
2. Running any model in any role, so which arrangement is better can be
   measured instead of assumed.

Both methods take the model as an argument rather than reading it from a fixed
"primary" or "verifier" slot. That is deliberate: the reviewer runs the same two
models in both roles — each scans the diff independently, and each verifies what
only the other reported — so a provider that hardcoded one model per stage could
not express the design.

The caller (``review.py``) knows only ``models``, ``scan`` and ``verify``. It
never learns which credential is behind them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema import Finding, ModelUsage, PRContext, Skill, Verdict


class ProviderUnavailable(Exception):
    """The provider cannot run: no credentials, model not entitled, and so on.

    When one model raises this, ``review.py`` continues with whatever the other
    produced and says so in the summary comment (graceful degradation,
    PRD §3.2).
    """


@runtime_checkable
class ReviewProvider(Protocol):
    """Runs a review model, whichever role it is playing this turn."""

    #: The models available, in configured order. The first is the one used
    #: when only a single scan is requested.
    models: list[str]

    #: Tokens and calls consumed so far, keyed by model. Reported in the
    #: summary so an adopter can see what a review actually costs them.
    usage: dict[str, ModelUsage]

    async def scan(self, model: str, ctx: PRContext, skill: Skill) -> list[Finding]:
        """Scan the whole PR once and return candidate findings. Favour recall.

        Scans must be **independent**: a model must not be shown what another
        model found. Independent agreement between two models is the strongest
        signal this design produces, and it is only worth anything if neither
        could have been influenced by the other.
        """
        ...

    async def verify(self, model: str, finding: Finding, ctx: PRContext) -> Verdict:
        """Judge a single finding in isolation. Favour precision.

        One finding, one verdict — never batch. ``model`` must be a model that
        did **not** report this finding; asking a model to check its own work
        measures nothing.
        """
        ...
