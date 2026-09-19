"""Preflight: a missing credential must cost seconds, not a half-finished suite.

The check is tested against a synthetic environment mapping, so these tests never touch the real process
environment and never read a key value.
"""

from __future__ import annotations

from opspilot.evals.preflight import any_ready, key_status, provider_of


def test_a_missing_key_names_the_variable_to_set() -> None:
    status = key_status("deepseek/deepseek-chat", environ={})

    assert not status.ready
    assert status.missing == ("DEEPSEEK_API_KEY",)
    assert "DEEPSEEK_API_KEY" in status.describe()
    assert "not configured" in status.describe()


def test_a_present_key_reports_ready_without_revealing_it() -> None:
    status = key_status(
        "mistral/mistral-small-latest", environ={"MISTRAL_API_KEY": "value-not-printed-anywhere"}
    )

    assert status.ready
    described = status.describe()
    assert "ready" in described
    assert "value-not-printed-anywhere" not in described


def test_azure_is_honest_about_needing_more_than_a_key() -> None:
    status = key_status("azure/gpt-4o", environ={"AZURE_API_KEY": "x"})

    assert not status.ready
    assert set(status.missing) == {"AZURE_API_BASE", "AZURE_API_VERSION"}


def test_an_unmapped_provider_is_allowed_through_but_flagged_as_unverified() -> None:
    """A local model server needs no key, so an unmapped provider must not be refused."""
    status = key_status("some-new-vendor/some-model", environ={})
    local = key_status("ollama/llama3", environ={})

    assert not status.known_provider
    assert "no key mapping known" in status.describe()
    assert status.ready
    assert local.ready and not local.known_provider


def test_provider_prefix_is_parsed_case_insensitively() -> None:
    assert provider_of("DeepSeek/deepseek-chat") == "deepseek"
    assert provider_of("no-slash-model") == ""


def test_a_fallback_chain_counts_as_usable_when_any_link_is_configured() -> None:
    environ = {"MISTRAL_API_KEY": "set"}

    assert any_ready(("deepseek/deepseek-chat", "mistral/mistral-small-latest"), environ=environ)
    assert not any_ready(("deepseek/deepseek-chat",), environ=environ)
    assert not any_ready((), environ=environ)
