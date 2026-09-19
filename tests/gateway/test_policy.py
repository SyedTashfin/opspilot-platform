from __future__ import annotations

import pytest

from opspilot.gateway.policy import ModelChain, ModelPolicy


def test_default_chain_deduplicates_and_keeps_order() -> None:
    policy = ModelPolicy.from_settings("a/a-1", "b/b-1", extra_fallbacks=("b/b-1", "c/c-1"))
    assert policy.default_chain.models == ("a/a-1", "b/b-1", "c/c-1")


def test_step_override_puts_override_first_and_keeps_fallbacks() -> None:
    policy = ModelPolicy.from_settings("a/a-1", "b/b-1", overrides={"diagnose": "strong/s-1"})
    assert policy.chain_for("diagnose").models == ("strong/s-1", "a/a-1", "b/b-1")


def test_unlisted_step_uses_the_default_chain() -> None:
    policy = ModelPolicy.from_settings("a/a-1", "b/b-1", overrides={"diagnose": "strong/s-1"})
    chain = policy.chain_for("classify")
    assert chain.models == ("a/a-1", "b/b-1")
    assert chain.step == "classify"


def test_override_that_repeats_the_primary_is_not_duplicated() -> None:
    policy = ModelPolicy.from_settings("a/a-1", "b/b-1", overrides={"diagnose": "b/b-1"})
    assert policy.chain_for("diagnose").models == ("b/b-1", "a/a-1")


def test_empty_chain_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="is empty"):
        ModelChain(step="diagnose", models=())


def test_all_models_is_unique_across_chains() -> None:
    policy = ModelPolicy.from_settings("a/a-1", "b/b-1", overrides={"diagnose": "a/a-1"})
    assert policy.all_models() == ("a/a-1", "b/b-1")
