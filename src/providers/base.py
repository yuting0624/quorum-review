"""The ReviewProvider protocol.

**This abstraction is the backbone of the project** (PRD §4.3). Two things
depend on it:

1. Swapping the primary implementation once Managed Agents reaches GA.
2. Running the roles in reverse (Gemini primary / Claude verifier and back)
   to find out empirically which ordering performs better.

The caller (``review.py``) only knows ``scan`` and ``verify``. It never learns
which model plays which role, or which credential is behind them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..schema import Finding, PRContext, Skill, Verdict


class ProviderUnavailable(Exception):
    """The provider cannot run: no credentials, model not entitled, and so on.

    When the verifier raises this, ``review.py`` continues with the primary
    model alone and says so in the summary comment (graceful degradation,
    PRD §3.2).
    """


@runtime_checkable
class ReviewProvider(Protocol):
    """Supplies both the primary scan and the second-opinion verification.

    Implementations must keep ``scan`` and ``verify`` in **separate sessions**.
    Never pass the primary model's reasoning (its title, body, or severity
    rationale) into ``verify``: doing so turns verification into agreement and
    destroys the only mechanism that removes false positives.
    """

    #: Identifier for the primary model, shown in the summary comment.
    primary_model: str
    #: Identifier for the verifier model, shown in the summary comment.
    verifier_model: str

    async def scan(self, ctx: PRContext, skill: Skill) -> list[Finding]:
        """Scan the whole PR once and return candidate findings. Favour recall."""
        ...

    async def verify(self, finding: Finding, ctx: PRContext) -> Verdict:
        """Judge a single finding in isolation. Favour precision.

        One finding, one verdict. Never batch several findings into one call.
        """
        ...
