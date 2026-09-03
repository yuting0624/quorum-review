"""The two-API-key provider, which the README points at as evidence.

`direct.py` exists so the comparison the project makes — one federated
credential against two vendor keys — can be read rather than argued. A control
case nobody exercises is not evidence of anything, and this one had no tests at
all: the claim rested on a file that might not have run since it was written.

The models are not called. What is checked is everything around them: that each
client refuses to be built without its own key, that a model reaches the right
vendor, that responses parse into the same shapes the Vertex provider produces,
and that usage is counted the same way.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from quorum_review import prompts
from quorum_review.providers import build_provider
from quorum_review.providers.base import ProviderUnavailable, ReviewProvider
from quorum_review.providers.direct import DirectProvider
from quorum_review.schema import Finding, PRContext, Skill

CTX = PRContext(
    owner="o",
    repo="r",
    number=1,
    head_sha="abc1234",
    base_sha="def5678",
    title="t",
    body="b",
    diff="diff --git a/a.py b/a.py\n+bad = 1\n",
)
SKILL = Skill("security-review", "CRITERIA")

FINDINGS_JSON = (
    '{"findings": [{"file_path": "a.py", "line": 1, "category": "security", '
    '"severity": "high", "title": "SQL injection", "body": "why", '
    '"code_snippet": "bad = 1"}]}'
)
VERDICT_JSON = '{"verdict": "confirmed", "reason": "traced it", "severity": "high"}'


def a_finding(**kwargs) -> Finding:
    base = {
        "file_path": "a.py",
        "line": 1,
        "category": "security",
        "severity": "high",
        "title": "t",
        "body": "b",
        "code_snippet": "s",
    }
    base.update(kwargs)
    return Finding(**base)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in (
        "GEMINI_API_KEY",
        "ANTHROPIC_API_KEY",
        "PRIMARY_MODEL",
        "VERIFIER_MODEL",
        "REVIEW_LANGUAGE",
        "QUORUM_MODE",
    ):
        monkeypatch.delenv(name, raising=False)


# -- the comparison the file exists to make --------------------------------


def test_it_satisfies_the_same_protocol_as_the_vertex_provider():
    """Otherwise the comparison is between two different things."""
    assert isinstance(DirectProvider(), ReviewProvider)


def test_it_declares_that_reviews_are_diff_only():
    assert DirectProvider().supports_repository_tools is False


def test_the_default_pair_uses_gemini_3_8_flash():
    assert DirectProvider().models == ["gemini-3.8-flash", "claude-sonnet-5"]


def test_neither_client_can_be_built_without_its_own_key():
    """Two secrets, from two vendors. That is the whole point being shown."""
    provider = DirectProvider()
    with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
        provider._gemini()
    with pytest.raises(ProviderUnavailable, match="ANTHROPIC_API_KEY"):
        provider._claude()


def test_a_missing_key_degrades_rather_than_crashing():
    """ProviderUnavailable is what lets the orchestrator carry on with the
    other model and name the one that could not run."""
    with pytest.raises(ProviderUnavailable):
        asyncio.run(DirectProvider().scan("gemini-3.6-flash", CTX, SKILL))


def test_mode_direct_selects_it():
    assert isinstance(build_provider("direct"), DirectProvider)


# -- routing ---------------------------------------------------------------


class FakeGemini:
    def __init__(self, text: str) -> None:
        self.calls: list[dict] = []
        self._text = text
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._call))

    async def _call(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            text=self._text,
            usage_metadata=SimpleNamespace(
                prompt_token_count=11, candidates_token_count=7
            ),
        )


class FakeClaudeStream:
    def __init__(self, text: str) -> None:
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_final_message(self):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self._text)],
            stop_reason="end_turn",
            usage=SimpleNamespace(
                input_tokens=13, output_tokens=5, cache_read_input_tokens=2
            ),
        )


class FakeClaude:
    def __init__(self, text: str) -> None:
        self.calls: list[dict] = []
        self._text = text
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeClaudeStream(self._text)


def wired(gemini_text: str = FINDINGS_JSON, claude_text: str = FINDINGS_JSON):
    provider = DirectProvider()
    gemini, claude = FakeGemini(gemini_text), FakeClaude(claude_text)
    provider._clients["gemini"] = gemini
    provider._clients["claude"] = claude
    return provider, gemini, claude


def test_a_claude_model_goes_to_anthropic_and_nowhere_else():
    provider, gemini, claude = wired()
    asyncio.run(provider.scan("claude-opus-5", CTX, SKILL))

    assert len(claude.calls) == 1
    assert gemini.calls == []


def test_a_gemini_model_goes_to_ai_studio_and_nowhere_else():
    provider, gemini, claude = wired()
    asyncio.run(provider.scan("gemini-3.6-flash", CTX, SKILL))

    assert len(gemini.calls) == 1
    assert claude.calls == []


def test_the_dispatch_is_on_the_model_id_not_a_configured_role():
    """Any model in any role is what makes the arrangement measurable, and it
    has to hold here too or the control case is not comparable."""
    provider, _gemini, claude = wired(claude_text=VERDICT_JSON)
    asyncio.run(provider.verify("claude-opus-5", a_finding(reported_by=["gemini"]), CTX))
    assert len(claude.calls) == 1


# -- the same shapes out ---------------------------------------------------


def test_a_scan_parses_into_findings():
    provider, _gemini, _claude = wired()
    findings = asyncio.run(provider.scan("gemini-3.6-flash", CTX, SKILL))

    assert len(findings) == 1
    assert findings[0].title == "SQL injection"
    assert findings[0].reported_by == ["gemini-3.6-flash"]


def test_a_verification_parses_into_a_verdict():
    provider, _gemini, _claude = wired(claude_text=VERDICT_JSON)
    verdict = asyncio.run(provider.verify("claude-opus-5", a_finding(), CTX))

    assert verdict.verdict == "confirmed"
    assert verdict.model == "claude-opus-5"


def test_prose_comes_back_unparsed():
    provider, _gemini, _claude = wired(claude_text="just some words")
    assert asyncio.run(provider.respond("claude-opus-5", "sys", "user")) == (
        "just some words"
    )


def test_usage_is_counted_per_model():
    """An adopter comparing the two arrangements needs both sides to count the
    same way, or the comparison is between two accounting conventions."""
    provider, _gemini, _claude = wired()
    asyncio.run(provider.scan("gemini-3.6-flash", CTX, SKILL))
    asyncio.run(provider.scan("claude-opus-5", CTX, SKILL))

    assert provider.usage["gemini-3.6-flash"].input_tokens == 11
    assert provider.usage["claude-opus-5"].input_tokens == 13
    assert provider.usage["claude-opus-5"].cached_input_tokens == 2


def test_the_prompts_are_the_ones_the_vertex_provider_sends():
    """If they diverged, comparing the two configurations would measure the
    prompts rather than the credential arrangement."""
    provider, gemini, _claude = wired()
    asyncio.run(provider.scan("gemini-3.6-flash", CTX, SKILL))

    sent = gemini.calls[0]
    assert sent["contents"] == prompts.scan_user(CTX)
    assert sent["config"].system_instruction == prompts.scan_system(SKILL, "")
    assert sent["config"].thinking_config.thinking_level == "HIGH"


def test_a_legacy_gemini_override_uses_its_default_thinking_configuration():
    """Gemini 2.5 accepts thinking_budget, not thinking_level."""
    provider, gemini, _claude = wired()
    asyncio.run(provider.scan("gemini-2.5-flash", CTX, SKILL))

    assert gemini.calls[0]["config"].thinking_config is None


def test_the_toolbox_is_accepted_and_ignored():
    """There is no tool loop here, deliberately. Refusing the argument would
    make the protocol a lie; pretending to use it would be worse."""
    provider, gemini, _claude = wired()
    asyncio.run(provider.scan("gemini-3.6-flash", CTX, SKILL, toolbox=object()))

    assert gemini.calls[0]["config"].tools is None
