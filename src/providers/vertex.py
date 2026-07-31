"""Gemini and Claude, both driven by one Google Cloud credential.

This module is the point of the whole project. Every other cross-model reviewer
we found stacks API keys from separate vendors; here the GitHub Actions OIDC
token is exchanged once for short-lived Google Cloud credentials, and *both*
models authenticate off those same Application Default Credentials. No
long-lived secret is stored in the repository, billing lands on one invoice, and
the code never leaves the Vertex region.

Which model plays which role is configuration, not structure. ``PRIMARY_MODEL``
and ``VERIFIER_MODEL`` are resolved to an engine by model-ID prefix, so running
the experiment in reverse — Claude scanning, Gemini verifying — is an
environment-variable change and nothing else.
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from .. import prompts
from ..schema import (
    FINDINGS_SCHEMA,
    VERDICT_SCHEMA,
    Finding,
    PRContext,
    Skill,
    Verdict,
    findings_from_payload,
    for_gemini,
    parse_json_object,
    verdict_from_payload,
)
from .base import ProviderUnavailable

# Defaults. Both are overridable; neither is load-bearing.
#
# NOTE ON THE GEMINI DEFAULT: model availability in Model Garden varies by
# project and by release channel, and the Gemini 3 line still carries `-preview`
# suffixes. Confirm what your project can actually call before relying on this
# value — `python -m src.review --list-models` prints the list.
DEFAULT_PRIMARY_MODEL = "gemini-3.1-pro-preview"
DEFAULT_VERIFIER_MODEL = "claude-opus-5"

# Output budgets. `max_tokens` on Claude caps thinking *plus* response text, and
# thinking is on by default on Opus 5 — a budget sized for the JSON alone
# truncates the answer mid-thought.
SCAN_MAX_TOKENS = 32_000
VERIFY_MAX_TOKENS = 8_000

# Verification is a short, well-scoped judgement on one finding, so it does not
# need deep reasoning. Scanning a whole diff does.
SCAN_EFFORT = "high"
VERIFY_EFFORT = "low"

_AUTH_HINTS = (
    "could not automatically determine credentials",
    "default credentials",
    "permission",
    "403",
    "unauthenticated",
    "401",
    "not found for api version",
    "404",
)


class _Engine(Protocol):
    """A single model behind a uniform 'return JSON matching this schema' call."""

    model: str

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        effort: str,
        max_tokens: int,
    ) -> str: ...


def _as_unavailable(model: str, error: Exception) -> Exception:
    """Reclassify credential and entitlement failures.

    ``ProviderUnavailable`` is what lets the orchestrator fall back to a
    primary-only review. Genuine bugs must keep propagating, so only errors that
    read like auth or missing-model problems are converted.
    """
    text = str(error).lower()
    if any(hint in text for hint in _AUTH_HINTS):
        return ProviderUnavailable(
            f"{model} is not callable with the current credentials: {error}"
        )
    return error


class _GeminiEngine:
    """Gemini on Vertex via the google-genai SDK."""

    def __init__(self, model: str, project: str, location: str) -> None:
        from google import genai

        self.model = model
        # vertexai=True is what routes this at Vertex rather than AI Studio.
        # Credentials come from ADC, which on Actions is the WIF-issued token.
        self._client = genai.Client(vertexai=True, project=project, location=location)

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        effort: str,
        max_tokens: int,
    ) -> str:
        from google.genai import types

        try:
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    # Gemini takes an OpenAPI subset, so the Claude-shaped
                    # schema is reduced here rather than maintained twice.
                    response_schema=for_gemini(schema),
                    max_output_tokens=max_tokens,
                ),
            )
        except Exception as error:  # noqa: BLE001 - reclassified below
            raise _as_unavailable(self.model, error) from error

        return response.text or ""


class _ClaudeEngine:
    """Claude on Vertex via the Anthropic SDK's Vertex client.

    The important detail is what is *absent*: no API key. ``AsyncAnthropicVertex``
    resolves Application Default Credentials, which is the same credential the
    Gemini client just used.
    """

    def __init__(self, model: str, project: str, region: str) -> None:
        from anthropic import AsyncAnthropicVertex

        self.model = model
        self._client = AsyncAnthropicVertex(project_id=project, region=region)

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any],
        effort: str,
        max_tokens: int,
    ) -> str:
        try:
            # Streaming even though the payload is small: a non-streaming call
            # with a large max_tokens can outlive the SDK's HTTP timeout.
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
                # The system prompt is identical across every verify call in a
                # run, so a cache breakpoint here is read back once per finding.
                # Vertex supports manual cache_control but not automatic caching.
                system=[
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user}],
            ) as stream:
                message = await stream.get_final_message()
        except Exception as error:  # noqa: BLE001 - reclassified below
            raise _as_unavailable(self.model, error) from error

        if message.stop_reason == "refusal":
            raise ValueError(f"{self.model} declined the request")
        if message.stop_reason == "max_tokens":
            raise ValueError(
                f"{self.model} hit max_tokens ({max_tokens}); raise the budget"
            )

        return "".join(block.text for block in message.content if block.type == "text")


class VertexProvider:
    """Runs both stages on Vertex AI off one credential."""

    def __init__(self) -> None:
        project = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if not project:
            raise ProviderUnavailable(
                "GOOGLE_CLOUD_PROJECT is not set; vertex mode needs a project ID"
            )

        self._project = project
        # 'global' is the recommended endpoint for both products. Claude's
        # entitlement in Model Garden can be region-scoped, so it gets its own
        # override — try us-east5 if a global call comes back 404.
        self._gemini_location = os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip()
        self._claude_region = os.getenv("CLAUDE_VERTEX_REGION", "global").strip()

        self.primary_model = os.getenv("PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL).strip()
        self.verifier_model = os.getenv("VERIFIER_MODEL", DEFAULT_VERIFIER_MODEL).strip()
        self.language = os.getenv("REVIEW_LANGUAGE", "").strip()

        self._engines: dict[str, _Engine] = {}

    def _engine(self, model: str) -> _Engine:
        """Resolve a model ID to an engine, constructing it on first use.

        Dispatching on the ID rather than on the role is what makes the two
        models swappable: set VERIFIER_MODEL to a Gemini ID and PRIMARY_MODEL to
        a Claude ID, and the roles reverse with no code change.
        """
        if model in self._engines:
            return self._engines[model]

        if model.startswith("claude"):
            engine: _Engine = _ClaudeEngine(model, self._project, self._claude_region)
        else:
            engine = _GeminiEngine(model, self._project, self._gemini_location)

        self._engines[model] = engine
        return engine

    async def scan(self, ctx: PRContext, skill: Skill) -> list[Finding]:
        raw = await self._engine(self.primary_model).complete(
            system=prompts.scan_system(skill, self.language),
            user=prompts.scan_user(ctx),
            schema=FINDINGS_SCHEMA,
            effort=SCAN_EFFORT,
            max_tokens=SCAN_MAX_TOKENS,
        )
        return findings_from_payload(parse_json_object(raw), self.primary_model)

    async def verify(self, finding: Finding, ctx: PRContext) -> Verdict:
        raw = await self._engine(self.verifier_model).complete(
            system=prompts.verify_system(self.language),
            user=prompts.verify_user(finding, ctx),
            schema=VERDICT_SCHEMA,
            effort=VERIFY_EFFORT,
            max_tokens=VERIFY_MAX_TOKENS,
        )
        return verdict_from_payload(parse_json_object(raw), self.verifier_model)

    def list_models(self) -> list[str]:
        """List the Gemini models this project can call.

        A setup aid: the correct model ID is the single most common thing to get
        wrong, and it differs per project and release channel.
        """
        from google import genai

        client = genai.Client(
            vertexai=True, project=self._project, location=self._gemini_location
        )
        return sorted(model.name or "" for model in client.models.list())
