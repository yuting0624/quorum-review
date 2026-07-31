"""The same two models, reached with two vendor API keys instead.

This exists so the tool can be tried without a Google Cloud project, and as the
control case for the argument the project is making: compare the environment
block here — two secrets from two vendors, two bills, two sets of terms — with
``vertex.py``, where the same review runs on one federated credential and
nothing long-lived is stored in the repository.

Kept deliberately plain. ``vertex.py`` is the file worth reading.
"""

from __future__ import annotations

import os
from typing import Any

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
from .vertex import (
    SCAN_EFFORT,
    SCAN_MAX_TOKENS,
    VERIFY_EFFORT,
    VERIFY_MAX_TOKENS,
)

DEFAULT_PRIMARY_MODEL = "gemini-3.1-pro-preview"
DEFAULT_VERIFIER_MODEL = "claude-opus-5"


class DirectProvider:
    """Gemini via an AI Studio key, Claude via an Anthropic key."""

    def __init__(self) -> None:
        self.primary_model = os.getenv("PRIMARY_MODEL", DEFAULT_PRIMARY_MODEL).strip()
        self.verifier_model = os.getenv("VERIFIER_MODEL", DEFAULT_VERIFIER_MODEL).strip()
        self.language = os.getenv("REVIEW_LANGUAGE", "").strip()
        self._clients: dict[str, Any] = {}

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
        schema: dict[str, Any],
        effort: str,
        max_tokens: int,
    ) -> str:
        if model.startswith("claude"):
            client = self._claude()
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                output_config={
                    "effort": effort,
                    "format": {"type": "json_schema", "schema": schema},
                },
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
            if message.stop_reason == "refusal":
                raise ValueError(f"{model} declined the request")
            return "".join(b.text for b in message.content if b.type == "text")

        from google.genai import types

        response = await self._gemini().aio.models.generate_content(
            model=model,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                response_schema=for_gemini(schema),
                max_output_tokens=max_tokens,
            ),
        )
        return response.text or ""

    async def scan(self, ctx: PRContext, skill: Skill) -> list[Finding]:
        raw = await self._complete(
            model=self.primary_model,
            system=prompts.scan_system(skill, self.language),
            user=prompts.scan_user(ctx),
            schema=FINDINGS_SCHEMA,
            effort=SCAN_EFFORT,
            max_tokens=SCAN_MAX_TOKENS,
        )
        return findings_from_payload(parse_json_object(raw), self.primary_model)

    async def verify(self, finding: Finding, ctx: PRContext) -> Verdict:
        raw = await self._complete(
            model=self.verifier_model,
            system=prompts.verify_system(self.language),
            user=prompts.verify_user(finding, ctx),
            schema=VERDICT_SCHEMA,
            effort=VERIFY_EFFORT,
            max_tokens=VERIFY_MAX_TOKENS,
        )
        return verdict_from_payload(parse_json_object(raw), self.verifier_model)
