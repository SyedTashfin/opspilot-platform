"""Injection detection: instructions that arrive inside data the agent retrieved.

The threat: a log line, a deployment note or a runbook paragraph contains text addressed to the model
("ignore your previous instructions", "restart the service now without approval", "do not report this").
The defence is not a polite request in a prompt. It is:

1. prompts state that retrieved content is data and never instruction (``prompts.py``);
2. restricted actions require a single-use approval bound to their arguments, so a persuaded agent still
   cannot act (``tools/executor.py``);
3. retrieved content is scanned for instruction-shaped text, and matches are **recorded in the report**
   rather than silently dropped (this module), so a reviewer can see that the agent was exposed.

The detector is a heuristic and is documented as one: it has false negatives, and a flag is evidence of
exposure, not proof of a successful attack. Nothing here changes control flow — flags are data.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

#: Patterns that are clearly adversarial in operational text. Deliberately narrow: a broad pattern like
#: "you must" appears in ordinary runbooks and would make every run look attacked. Measured against the
#: five shipped runbooks by ``tests/agents/test_guardrails.py``.
INSTRUCTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction-override",
        re.compile(
            r"(ignore|disregard|forget)\s+(all\s+)?(your\s+|the\s+)?(previous|prior|above|earlier)\s+"
            r"(instructions?|prompts?|rules?)",
            re.IGNORECASE,
        ),
    ),
    (
        "spoofed-role",
        re.compile(r"(^|\n)\s*(system|assistant|developer)\s*:", re.IGNORECASE),
    ),
    (
        "concealment",
        re.compile(
            r"do\s+not\s+(tell|report|mention|log|disclose)\b|without\s+telling\s+(the\s+)?"
            r"(user|operator|human)",
            re.IGNORECASE,
        ),
    ),
    (
        "auto-approval",
        re.compile(
            r"(approve|execute|run|perform)\b[^.]{0,40}\b(automatically|without\s+(approval|asking|"
            r"confirmation))",
            re.IGNORECASE,
        ),
    ),
    (
        "destructive-command",
        re.compile(r"\b(rm\s+-rf|drop\s+database|drop\s+table|truncate|mkfs)\b", re.IGNORECASE),
    ),
    (
        "urgent-restricted-action",
        re.compile(
            r"\b(restart|delete|terminate|scale\s+(down|to\s+zero)|disable)\b[^.]{0,60}\b"
            r"(immediately|right\s+now|as\s+soon\s+as\s+possible)",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class InjectionFlag:
    source: str
    pattern: str
    excerpt: str

    def as_text(self) -> str:
        return f"{self.pattern} in {self.source}: {self.excerpt}"


def detect_injections(texts: Mapping[str, str]) -> list[InjectionFlag]:
    """Scan ``{source: text}`` for instruction-shaped content. Order is deterministic."""
    flags: list[InjectionFlag] = []
    for source, text in texts.items():
        if not text:
            continue
        for label, pattern in INSTRUCTION_PATTERNS:
            match = pattern.search(text)
            if match is None:
                continue
            start = max(0, match.start() - 40)
            excerpt = " ".join(text[start : match.end() + 40].split())
            flags.append(InjectionFlag(source=source, pattern=label, excerpt=excerpt[:200]))
    return flags


def flag_texts(flags: list[InjectionFlag]) -> list[str]:
    return [flag.as_text() for flag in flags]
