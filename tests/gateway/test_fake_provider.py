from __future__ import annotations

import pytest
from pydantic import BaseModel

from opspilot.gateway.errors import ProviderError
from opspilot.gateway.providers.fake import FakeProvider, approximate_tokens
from opspilot.gateway.types import Message, ModelRequest


class Incident(BaseModel):
    root_cause: str
    confidence: float
    evidence: list[str]
    retry: bool


def _request(response_model: type[BaseModel] | None = None) -> ModelRequest:
    return ModelRequest(
        step="diagnose",
        messages=(Message.system("be terse"), Message.user("why is the API slow?")),
        response_model=response_model,
    )


async def test_structured_response_validates_against_the_schema() -> None:
    provider = FakeProvider()
    result = await provider.complete("fake/fake-1", _request(Incident))
    assert isinstance(result.parsed, Incident)
    assert result.parsed.confidence == 0.1
    assert result.usage.input_tokens >= 1
    assert result.usage.output_tokens >= 1


async def test_same_request_yields_the_same_output() -> None:
    first = await FakeProvider().complete("fake/fake-1", _request(Incident))
    second = await FakeProvider().complete("fake/fake-1", _request(Incident))
    assert first.text == second.text
    assert first.usage == second.usage


async def test_plain_completion_is_also_deterministic() -> None:
    result = await FakeProvider().complete("fake/fake-1", _request())
    assert result.parsed is None
    assert result.text == "fake completion for step diagnose"


async def test_scripted_failure_then_success() -> None:
    provider = FakeProvider(fail_first=1)
    with pytest.raises(ProviderError):
        await provider.complete("fake/fake-1", _request())
    result = await provider.complete("fake/fake-1", _request())
    assert result.text != ""


async def test_fail_always_never_succeeds() -> None:
    provider = FakeProvider(fail_always=True)
    for _ in range(3):
        with pytest.raises(ProviderError):
            await provider.complete("fake/fake-1", _request())


def test_only_fake_models_are_supported() -> None:
    provider = FakeProvider()
    assert provider.supports("fake/fake-1")
    assert not provider.supports("deepseek/deepseek-chat")


def test_token_approximation_grows_with_length() -> None:
    assert approximate_tokens("x" * 400) > approximate_tokens("x" * 40)
    assert approximate_tokens("") == 1
