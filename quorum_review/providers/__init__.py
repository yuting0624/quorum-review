"""Review providers, selected by ``QUORUM_MODE``.

- ``vertex``  Gemini and Claude driven by a single Google Cloud credential.
              This is the configuration the project exists to demonstrate.
- ``direct``  An AI Studio Gemini key plus an Anthropic API key, for people who
              want to try the tool without setting up Google Cloud.
"""

from __future__ import annotations

import os

from .base import ProviderUnavailable, ReviewProvider


def build_provider(mode: str | None = None) -> ReviewProvider:
    """Return the provider for the requested mode.

    Imports are deferred so that someone running in ``direct`` mode does not
    need ``anthropic[vertex]`` and ``google-genai`` installed.
    """
    mode = (mode or os.getenv("QUORUM_MODE", "vertex")).strip().lower()

    if mode == "vertex":
        from .vertex import VertexProvider

        return VertexProvider()
    if mode == "direct":
        from .direct import DirectProvider

        return DirectProvider()

    raise ValueError(f"unknown QUORUM_MODE {mode!r} (expected 'vertex' or 'direct')")


__all__ = ["ProviderUnavailable", "ReviewProvider", "build_provider"]
