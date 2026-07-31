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
    ModelUsage,
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
# project and by release channel, and parts of the Gemini 3 line still carry
# `-preview` suffixes. Confirm what your project can actually call before
# relying on this value — `python -m src.review --list-models` prints the list.
DEFAULT_PRIMARY_MODEL = "gemini-3.6-flash"
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

# Prose answers — a question in a thread, a proposed criteria change. Scoped
# but open-ended: someone is pushing back and deserves a considered reply
# rather than a restatement.
PROSE_EFFORT = "medium"
PROSE_MAX_TOKENS = 8_000

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
    usage: ModelUsage

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
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
        self.usage = ModelUsage()
        # vertexai=True is what routes this at Vertex rather than AI Studio.
        # Credentials come from ADC, which on Actions is the WIF-issued token.
        self._client = genai.Client(vertexai=True, project=project, location=location)

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
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
                    # No schema means a prose answer — used when replying to a
                    # question in a thread rather than reporting findings.
                    response_mime_type="application/json" if schema else "text/plain",
                    # Gemini takes an OpenAPI subset, so the Claude-shaped
                    # schema is reduced here rather than maintained twice.
                    response_schema=for_gemini(schema) if schema else None,
                    max_output_tokens=max_tokens,
                ),
            )
        except Exception as error:  # noqa: BLE001 - reclassified below
            raise _as_unavailable(self.model, error) from error

        meta = getattr(response, "usage_metadata", None)
        self.usage.add(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
            cached_input_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
        )
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
        self.usage = ModelUsage()
        self._client = AsyncAnthropicVertex(project_id=project, region=region)

    async def complete(
        self,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        effort: str,
        max_tokens: int,
    ) -> str:
        output_config: dict[str, Any] = {"effort": effort}
        if schema:
            output_config["format"] = {"type": "json_schema", "schema": schema}

        try:
            # Streaming even though the payload is small: a non-streaming call
            # with a large max_tokens can outlive the SDK's HTTP timeout.
            async with self._client.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                output_config=output_config,
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

        used = message.usage
        self.usage.add(
            input_tokens=getattr(used, "input_tokens", 0) or 0,
            output_tokens=getattr(used, "output_tokens", 0) or 0,
            cached_input_tokens=getattr(used, "cache_read_input_tokens", 0) or 0,
        )

        if message.stop_reason == "refusal":
            raise ValueError(f"{self.model} declined the request")
        if message.stop_reason == "max_tokens":
            raise ValueError(
                f"{self.model} hit max_tokens ({max_tokens}); raise the budget"
            )

        return "".join(block.text for block in message.content if block.type == "text")


class VertexProvider:
    """Runs every review model on Vertex AI off one credential."""

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

        # Two configured slots, but both models do both jobs. The names are
        # kept because they are the documented inputs; the order only decides
        # which one runs alone when a single-model review is requested.
        first = os.getenv("PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL).strip()
        second = os.getenv("VERIFIER_MODEL", DEFAULT_VERIFIER_MODEL).strip()
        self.models = [m for m in dict.fromkeys([first, second]) if m]

        self.language = os.getenv("REVIEW_LANGUAGE", "").strip()
        self._engines: dict[str, _Engine] = {}

    @property
    def usage(self) -> dict[str, ModelUsage]:
        """Per-model token and call counts, for models actually used."""
        return {model: engine.usage for model, engine in self._engines.items()}

    def _engine(self, model: str) -> _Engine:
        """Resolve a model ID to an engine, constructing it on first use.

        Dispatching on the ID rather than on the role is what lets any model
        play any part: the same lookup serves a scan and a verification.
        """
        if model in self._engines:
            return self._engines[model]

        if model.startswith("claude"):
            engine: _Engine = _ClaudeEngine(model, self._project, self._claude_region)
        else:
            engine = _GeminiEngine(model, self._project, self._gemini_location)

        self._engines[model] = engine
        return engine

    async def scan(self, model: str, ctx: PRContext, skill: Skill) -> list[Finding]:
        raw = await self._engine(model).complete(
            system=prompts.scan_system(skill, self.language),
            user=prompts.scan_user(ctx),
            schema=FINDINGS_SCHEMA,
            effort=SCAN_EFFORT,
            max_tokens=SCAN_MAX_TOKENS,
        )
        return findings_from_payload(parse_json_object(raw), model)

    async def respond(
        self, model: str, system: str, user: str, max_tokens: int = PROSE_MAX_TOKENS
    ) -> str:
        return await self._engine(model).complete(
            system=system,
            user=user,
            schema=None,
            effort=PROSE_EFFORT,
            max_tokens=max_tokens,
        )

    async def verify(self, model: str, finding: Finding, ctx: PRContext) -> Verdict:
        raw = await self._engine(model).complete(
            system=prompts.verify_system(self.language),
            user=prompts.verify_user(finding, ctx),
            schema=VERDICT_SCHEMA,
            effort=VERIFY_EFFORT,
            max_tokens=VERIFY_MAX_TOKENS,
        )
        return verdict_from_payload(parse_json_object(raw), model)

    def list_models(self) -> list[str]:
        """List the Gemini models this project can call.

        A setup aid: the correct model ID is the single most common thing to get
        wrong, and it differs per project and release channel.
        """
        from google import genai

        client = genai.Client(
            vertexai=True, project=self._project, location=self._gemini_location
        )
        # The API returns fully qualified names like
        # `publishers/google/models/gemini-x`; strip the prefix so what is
        # printed can be pasted straight into PRIMARY_MODEL.
        return sorted(
            (model.name or "").rsplit("/", 1)[-1]
            for model in client.models.list()
            if model.name
        )
