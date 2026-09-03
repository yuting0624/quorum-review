"""The same two models, reached through each vendor's own API instead.

This exists so the tool can be tried without a Google Cloud project, and as the
control case for the argument the project makes: compare the environment block
here — two vendors, two bills, two sets of terms — with ``vertex.py``, where
the same review runs on one federated credential.

The argument used to be sharper than it is. Until June 2026 this file needed
two long-lived API keys and ``vertex.py`` needed none, which was the whole
comparison. Anthropic then shipped workload identity federation, so the Claude
half of a two-vendor setup can be keyless too — a GitHub Actions OIDC token
exchanged for a short-lived credential. ``GEMINI_API_KEY`` below is still a
stored key, because the AI Studio API takes one.

So what ``vertex.py`` buys is one trust relationship rather than zero stored
secrets: one issuer, one set of claim conditions, one place to get wrong.
Narrower, and still the reason to read that file rather than this one.
"""

from __future__ import annotations

import os
from typing import Any

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
from .vertex import (
    DEFAULT_PRIMARY_MODEL,
    DEFAULT_VERIFIER_MODEL,
    PROSE_EFFORT,
    PROSE_MAX_TOKENS,
    SCAN_EFFORT,
    SCAN_MAX_TOKENS,
    VERIFY_EFFORT,
    VERIFY_MAX_TOKENS,
)


def _output_config(effort: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    """No schema means a prose answer, used when replying to a question."""
    config: dict[str, Any] = {"effort": effort}
    if schema:
        config["format"] = {"type": "json_schema", "schema": schema}
    return config


class DirectProvider:
    """Gemini via an AI Studio key, Claude via an Anthropic key."""

    def __init__(self) -> None:
        first = os.getenv("PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL).strip()
        second = os.getenv("VERIFIER_MODEL", DEFAULT_VERIFIER_MODEL).strip()
        self.models = [m for m in dict.fromkeys([first, second]) if m]
        self.language = os.getenv("REVIEW_LANGUAGE", "").strip()
        self._clients: dict[str, Any] = {}
        self.usage: dict[str, ModelUsage] = {}

    def _record(self, model: str, **tokens: int) -> None:
        self.usage.setdefault(model, ModelUsage()).add(**tokens)

    def _gemini(self) -> Any:
        if "gemini" not in self._clients:
            api_key = os.getenv("GEMINI_API_KEY", "").strip()
            if not api_key:
                raise ProviderUnavailable("GEMINI_API_KEY is not set")
            from google import genai

            self._clients["gemini"] = genai.Client(api_key=api_key)
        return self._clients["gemini"]

    def _claude(self) -> Any:
        if "claude" not in self._clients:
            api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                raise ProviderUnavailable("ANTHROPIC_API_KEY is not set")
            from anthropic import AsyncAnthropic

            self._clients["claude"] = AsyncAnthropic(api_key=api_key)
        return self._clients["claude"]

    async def _complete(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any] | None,
        effort: str,
        max_tokens: int,
    ) -> str:
        if model.startswith("claude"):
            client = self._claude()
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                output_config=_output_config(effort, schema),
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
            self._record(
                model,
                input_tokens=getattr(message.usage, "input_tokens", 0) or 0,
                output_tokens=getattr(message.usage, "output_tokens", 0) or 0,
                cached_input_tokens=getattr(message.usage, "cache_read_input_tokens", 0)
                or 0,
            )
            if message.stop_reason == "refusal":
                raise ValueError(f"{model} declined the request")
            return "".join(b.text for b in message.content if b.type == "text")

        from google.genai import types

        response = await self._gemini().aio.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json" if schema else "text/plain",
                response_schema=for_gemini(schema) if schema else None,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_level=effort),
            ),
        )
        meta = getattr(response, "usage_metadata", None)
        self._record(
            model,
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
        )
        return response.text or ""

    async def respond(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int = PROSE_MAX_TOKENS,
        toolbox: Any | None = None,
    ) -> str:
        return await self._complete(
            model=model,
            system=system,
            user=user,
            schema=None,
            effort=PROSE_EFFORT,
            max_tokens=max_tokens,
        )

    # `toolbox` is accepted and ignored. This provider exists to show that the
    # arrangement is not tied to Vertex, not to be the one people run, and a
    # tool loop would be a second full implementation of the interesting part.
    # Reviews here are diff-only; the summary says so.
    async def scan(
        self,
        model: str,
        ctx: PRContext,
        skill: Skill,
        toolbox: Any | None = None,
    ) -> list[Finding]:
        raw = await self._complete(
            model=model,
            system=prompts.scan_system(skill, self.language),
            user=prompts.scan_user(ctx),
            schema=FINDINGS_SCHEMA,
            effort=SCAN_EFFORT,
            max_tokens=SCAN_MAX_TOKENS,
        )
        return findings_from_payload(parse_json_object(raw), model)

    async def verify(
        self,
        model: str,
        finding: Finding,
        ctx: PRContext,
        toolbox: Any | None = None,
    ) -> Verdict:
        raw = await self._complete(
            model=model,
            system=prompts.verify_system(self.language),
            user=prompts.verify_user(finding, ctx),
            schema=VERDICT_SCHEMA,
            effort=VERIFY_EFFORT,
            max_tokens=VERIFY_MAX_TOKENS,
        )
        return verdict_from_payload(parse_json_object(raw), model)
