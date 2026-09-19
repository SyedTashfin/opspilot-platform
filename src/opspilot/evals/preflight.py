"""Provider key preflight: say what is missing *before* a run starts, not midway through it.

Why this exists: ``--provider configured`` used to reach the first model call and fail there, leaving a
half-finished suite and a stack trace that says nothing useful. A suite that needs a credential should
state which credential, and stop, before it spends a second or a cent.

Keys are read from the environment by LiteLLM. This module never reads, stores or prints a key value —
only whether the variable is set.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

#: Which environment variables each provider family needs. Azure needs more than a key, which is exactly
#: the sort of thing worth saying out loud before a run rather than discovering at the first call.
PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "azure": ("AZURE_API_KEY", "AZURE_API_BASE", "AZURE_API_VERSION"),
}


@dataclass(frozen=True, slots=True)
class KeyStatus:
    provider: str
    model: str
    required: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def ready(self) -> bool:
        """Whether nothing we can check is missing.

        True for an unmapped provider on purpose: a local model server (``ollama/...``) needs no key at
        all, and blocking it because we have no mapping would be a false refusal. The unmapped case is
        flagged as unverified instead of refused — see ``known_provider``.
        """
        return not self.missing

    @property
    def known_provider(self) -> bool:
        """False when we have no mapping: the call may still work, we just cannot promise it will."""
        return bool(self.required)

    def describe(self) -> str:
        if not self.known_provider:
            return (
                f"{self.model}: no key mapping known for provider {self.provider!r}; "
                "the run will fail at the first call if that provider needs credentials"
            )
        if self.ready:
            return f"{self.model}: ready ({', '.join(self.required)} set)"
        return f"{self.model}: not configured — set {', '.join(self.missing)}"


def provider_of(model: str) -> str:
    return model.split("/", 1)[0].lower() if "/" in model else ""


def key_status(model: str, *, environ: Mapping[str, str] | None = None) -> KeyStatus:
    source = environ if environ is not None else os.environ
    provider = provider_of(model)
    required = PROVIDER_ENV.get(provider, ())
    missing = tuple(name for name in required if not source.get(name))
    return KeyStatus(provider=provider, model=model, required=required, missing=missing)


def any_ready(models: tuple[str, ...], *, environ: Mapping[str, str] | None = None) -> bool:
    """Whether at least one model in a fallback chain has its key configured."""
    return any(key_status(model, environ=environ).ready for model in models)
