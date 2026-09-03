"""Gemini request details that are easy to lose during a model upgrade."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from google.genai import types

from quorum_review.providers.vertex import _GeminiEngine
from quorum_review.schema import ModelUsage


class FakeModels:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.responses)


class FakeToolbox:
    exhausted = False

    def run(self, name, args):
        assert name == "read_file"
        assert args == {"path": "README.md"}
        return "file contents"


def response(content, function_calls):
    return SimpleNamespace(
        candidates=[SimpleNamespace(content=content)],
        function_calls=function_calls,
        usage_metadata=None,
    )


def test_tool_responses_preserve_the_function_call_id_and_effort():
    call = types.FunctionCall(
        id="call-1", name="read_file", args={"path": "README.md"}
    )
    model_turn = types.Content(
        role="model", parts=[types.Part(function_call=call)]
    )
    done_turn = types.Content(role="model", parts=[types.Part(text="done")])
    models = FakeModels(
        [response(model_turn, [call]), response(done_turn, [])]
    )

    engine = object.__new__(_GeminiEngine)
    engine.model = "gemini-3.8-flash"
    engine.usage = ModelUsage()
    engine._client = SimpleNamespace(aio=SimpleNamespace(models=models))
    contents = [types.Content(role="user", parts=[types.Part(text="review")])]

    asyncio.run(engine._explore(contents, "system", "high", 1000, FakeToolbox()))

    tool_response = contents[2].parts[0].function_response
    assert tool_response.id == "call-1"
    assert tool_response.name == "read_file"
    assert tool_response.response == {"output": "file contents"}
    assert all(
        call["config"].thinking_config.thinking_level == "HIGH"
        for call in models.calls
    )
