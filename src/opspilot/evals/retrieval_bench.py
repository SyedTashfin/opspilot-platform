"""The measurement that decides ADR-017: does lexical retrieval find the right runbook?

ADR-017 chose BM25 over embeddings and said the decision would be revisited only if the evaluation suite
showed a retrieval gap. This module is that check, and it is built to be unkind to the choice: the queries
are paraphrases that deliberately avoid the runbooks' own vocabulary — "the box is pegged and everything
crawls" rather than "high CPU utilisation". If lexical retrieval only works when the query and the runbook
share words, this number is where that shows up.

Reported, not gated: a retrieval score is a property of the index, not a structural guarantee, and
ADR-017 says the number decides — not that it must be perfect before the project can proceed.
"""

from __future__ import annotations

from dataclasses import dataclass

from opspilot.retrieval.runbooks import RunbookIndex


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    query: str
    expected_document: str
    note: str = ""


#: Paraphrased symptoms, written the way a person describes a problem rather than the way a runbook is
#: titled. `expected_document` is the runbook a competent operator would want.
PARAPHRASE_QUERIES: tuple[RetrievalQuery, ...] = (
    RetrievalQuery(
        query="requests are queueing because we ran out of database connections",
        expected_document="database-connectivity.md",
    ),
    RetrievalQuery(
        query="everything got slow right after someone pushed a new release",
        expected_document="deployment-rollback.md",
    ),
    RetrievalQuery(
        query="clients are getting five hundred responses",
        expected_document="http-5xx.md",
    ),
    RetrievalQuery(
        query="we keep calling a service that cannot keep up and it makes everything slower",
        expected_document="dependency-timeout.md",
    ),
    RetrievalQuery(
        query="the box is pegged and everything crawls",
        expected_document="high-cpu.md",
    ),
    RetrievalQuery(
        query="retry amplification against a slow dependency",
        expected_document="dependency-timeout.md",
        note="shares vocabulary with the runbook: the easy case, included for contrast",
    ),
    RetrievalQuery(
        query="connection pool exhausted",
        expected_document="database-connectivity.md",
        note="partial vocabulary overlap",
    ),
)


def bench(index: RunbookIndex, *, limit: int = 3) -> dict[str, object]:
    """Run every paraphrase query and report hit@1, hit@limit and the misses."""
    rows: list[dict[str, object]] = []
    for item in PARAPHRASE_QUERIES:
        hits = index.search(item.query, limit=limit)
        documents = [chunk.document for chunk in hits]
        rows.append(
            {
                "query": item.query,
                "expected": item.expected_document,
                "returned": documents,
                "hit_at_1": bool(documents) and documents[0] == item.expected_document,
                "hit_at_k": item.expected_document in documents,
                "note": item.note,
            }
        )
    total = len(rows)
    hit_at_1 = sum(1 for row in rows if row["hit_at_1"])
    hit_at_k = sum(1 for row in rows if row["hit_at_k"])
    misses = [row["query"] for row in rows if not row["hit_at_k"]]
    return {
        "queries": total,
        "limit": limit,
        "hit_at_1": hit_at_1,
        "hit_at_1_rate": round(hit_at_1 / total, 4) if total else 0.0,
        "hit_at_k": hit_at_k,
        "hit_at_k_rate": round(hit_at_k / total, 4) if total else 0.0,
        "misses": misses,
        "rows": rows,
        "method": (
            "BM25 over heading-delimited chunks; paraphrased queries avoid the runbooks' own vocabulary. "
            "hit@k counts the expected runbook appearing anywhere in the top k chunks"
        ),
        "decision": (
            "ADR-017 keeps lexical retrieval while hit@k stays high; misses here are the evidence that "
            "would justify embeddings"
        ),
    }
