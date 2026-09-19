"""Runbook loading, chunking and lexical retrieval.

Each chunk carries a stable citation id and a content hash, so a diagnosis can cite evidence that a
reviewer can verify byte for byte. Retrieval is deterministic: same query, same ordering, no model
required — which also means the evaluation suite can measure retrieval in isolation from generation.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOKEN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]+")
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "without",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "from",
        "at",
        "by",
        "as",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "if",
        "then",
        "than",
        "into",
        "over",
        "under",
        "not",
        "no",
        "do",
        "does",
        "did",
        "can",
        "could",
        "should",
        "would",
        "may",
        "might",
        "must",
        "will",
    ]
)
# Section headings that are useful for navigation but meaningless as retrieval terms.
BOILERPLATE_HEADINGS = frozenset(
    {"symptoms", "diagnosis", "likely causes", "remediation", "verification", "escalation"}
)


def _tokens(text: str) -> list[str]:
    return [token for token in TOKEN_PATTERN.findall(text.lower()) if token not in STOPWORDS]


@dataclass(frozen=True, slots=True)
class RunbookChunk:
    citation_id: str
    document: str
    heading: str
    text: str
    content_hash: str
    tokens: tuple[str, ...]

    def as_evidence(self) -> dict[str, Any]:
        return {
            "citation_id": self.citation_id,
            "document": self.document,
            "heading": self.heading,
            "content_hash": self.content_hash,
            "excerpt": self.text.strip()[:600],
        }


@dataclass
class RunbookIndex:
    """Inverted index with BM25-style scoring, computed at load time.

    Small enough to hold in memory (a few dozen documents), and deterministic by construction. The
    scoring parameters are the standard BM25 defaults rather than tuned constants, because tuning
    without an evaluation set would be guessing with extra steps.
    """

    chunks: list[RunbookChunk] = field(default_factory=list)
    k1: float = 1.5
    b: float = 0.75
    _document_frequency: Counter[str] = field(default_factory=Counter, init=False)
    _chunk_lengths: list[int] = field(default_factory=list, init=False)
    _average_length: float = field(default=1.0, init=False)

    def __post_init__(self) -> None:
        for chunk in self.chunks:
            for token in set(chunk.tokens):
                self._document_frequency[token] += 1
        self._chunk_lengths = [len(chunk.tokens) for chunk in self.chunks]
        total = sum(self._chunk_lengths)
        self._average_length = (total / len(self.chunks)) if self.chunks else 1.0

    def __len__(self) -> int:
        return len(self.chunks)

    def _idf(self, token: str) -> float:
        n = len(self.chunks)
        df = self._document_frequency.get(token, 0)
        if n == 0 or df == 0:
            return 0.0
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def score(self, query: str, chunk: RunbookChunk, index: int) -> float:
        query_tokens = _tokens(query)
        if not query_tokens or not chunk.tokens:
            return 0.0
        counts = Counter(chunk.tokens)
        length = self._chunk_lengths[index]
        score = 0.0
        for token in query_tokens:
            frequency = counts.get(token, 0)
            if frequency == 0:
                continue
            denominator = frequency + self.k1 * (
                1 - self.b + self.b * (length / (self._average_length or 1.0))
            )
            score += self._idf(token) * (frequency * (self.k1 + 1)) / denominator
        # A heading match is a strong signal for a runbook corpus and cheap to honour.
        if any(token in _tokens(chunk.heading) for token in query_tokens):
            score *= 1.35
        return score

    def search(self, query: str, *, limit: int = 3, min_score: float = 0.0) -> list[RunbookChunk]:
        scored = [(self.score(query, chunk, index), index, chunk) for index, chunk in enumerate(self.chunks)]
        scored = [row for row in scored if row[0] > min_score]
        # Deterministic ordering: score descending, then citation id, so ties never reorder
        # run to run.
        scored.sort(key=lambda row: (-row[0], row[2].citation_id))
        return [chunk for _, _, chunk in scored[:limit]]


HEADING_PATTERN = re.compile(r"^#{1,3} (.+)$", re.MULTILINE)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def chunk_markdown(path: Path, document: str) -> list[RunbookChunk]:
    """Split a runbook by heading, keeping the front-matter title on every chunk for context."""
    raw = path.read_text(encoding="utf-8")
    title = document
    body = raw
    if raw.startswith("---"):
        parts = raw.split("---", 2)
        if len(parts) == 3:
            front_matter, body = parts[1], parts[2]
            for line in front_matter.splitlines():
                if line.lower().startswith("title:"):
                    title = line.split(":", 1)[1].strip()
                    break

    matches = list(HEADING_PATTERN.finditer(body))
    chunks: list[RunbookChunk] = []
    for position, match in enumerate(matches):
        heading = match.group(1).strip()
        start = match.end()
        end = matches[position + 1].start() if position + 1 < len(matches) else len(body)
        text = body[start:end].strip()
        if not text:
            continue
        citation_id = f"{document}#{heading.lower().replace(' ', '-')}"
        chunk_text = f"{title} :: {heading}\n{text}"
        chunks.append(
            RunbookChunk(
                citation_id=citation_id,
                document=document,
                heading=heading,
                text=chunk_text,
                content_hash=_content_hash(chunk_text),
                tokens=tuple(_tokens(chunk_text)),
            )
        )
    return chunks


def load_runbooks(directory: str | Path) -> RunbookIndex:
    path = Path(directory)
    if not path.exists():
        msg = f"runbook directory {path} does not exist"
        raise FileNotFoundError(msg)
    chunks: list[RunbookChunk] = []
    for file in sorted(path.rglob("*.md")):
        chunks.extend(chunk_markdown(file, document=file.name))
    if not chunks:
        msg = f"no runbook chunks found under {path}"
        raise ValueError(msg)
    return RunbookIndex(chunks=chunks)
