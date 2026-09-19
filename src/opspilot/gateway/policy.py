"""Model policy: which model serves which step, and what to try when it fails.

Routing is configuration. An agent declares a *policy*; the policy resolves to an ordered chain of
models. Nothing in the agent code names a vendor, so moving a step to a cheaper or stronger model is
a configuration change with a measurable effect on the evaluation suite.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelChain:
    """Ordered models for one step. The first entry is primary; the rest are fallbacks."""

    step: str
    models: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.models:
            msg = f"model chain for step {self.step!r} is empty"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ModelPolicy:
    default_chain: ModelChain
    step_chains: Mapping[str, ModelChain]

    def chain_for(self, step: str) -> ModelChain:
        return self.step_chains.get(step, ModelChain(step=step, models=self.default_chain.models))

    @staticmethod
    def from_settings(
        default_model: str,
        fallback_model: str,
        overrides: Mapping[str, str] | None = None,
        extra_fallbacks: Iterable[str] = (),
    ) -> ModelPolicy:
        base = tuple(dict.fromkeys((default_model, fallback_model, *extra_fallbacks)))
        step_chains = {}
        for step, primary in (overrides or {}).items():
            models = (primary, *(model for model in base if model != primary))
            step_chains[step] = ModelChain(step=step, models=models)
        return ModelPolicy(
            default_chain=ModelChain(step="default", models=base),
            step_chains=step_chains,
        )

    def all_models(self) -> Sequence[str]:
        seen: dict[str, None] = {}
        for chain in (self.default_chain, *self.step_chains.values()):
            for model in chain.models:
                seen.setdefault(model, None)
        return tuple(seen)
