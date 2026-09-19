from __future__ import annotations

from pathlib import Path

from opspilot.retrieval.runbooks import load_runbooks

RUNBOOK_DIR = Path(__file__).resolve().parents[2] / "runbooks"


def test_every_shipped_runbook_is_indexed() -> None:
    index = load_runbooks(RUNBOOK_DIR)
    documents = {chunk.document for chunk in index.chunks}
    assert documents == {path.name for path in RUNBOOK_DIR.glob("*.md")}
    assert len(index) >= len(documents) * 2


def test_chunks_carry_a_citable_identity_and_a_content_hash() -> None:
    index = load_runbooks(RUNBOOK_DIR)
    citation_ids = [chunk.citation_id for chunk in index.chunks]
    assert len(set(citation_ids)) == len(citation_ids)
    assert all(chunk.document and chunk.heading and chunk.text for chunk in index.chunks)
    assert all(len(chunk.content_hash) >= 12 for chunk in index.chunks)


def test_indexing_is_deterministic() -> None:
    first = load_runbooks(RUNBOOK_DIR)
    second = load_runbooks(RUNBOOK_DIR)
    assert [chunk.citation_id for chunk in first.chunks] == [
        chunk.citation_id for chunk in second.chunks
    ]
    assert [chunk.content_hash for chunk in first.chunks] == [
        chunk.content_hash for chunk in second.chunks
    ]


def test_a_slow_dependency_symptom_finds_the_dependency_runbook() -> None:
    index = load_runbooks(RUNBOOK_DIR)
    hits = index.search("retry amplification against a slow dependency timeout", limit=3)
    assert hits
    assert hits[0].document == "dependency-timeout.md"


def test_a_database_connectivity_symptom_finds_the_database_runbook() -> None:
    index = load_runbooks(RUNBOOK_DIR)
    hits = index.search("connection pool exhausted database connection refused", limit=3)
    assert hits
    assert hits[0].document == "database-connectivity.md"


def test_search_respects_the_limit_and_scores_descending() -> None:
    index = load_runbooks(RUNBOOK_DIR)
    hits = index.search("high cpu saturation throttling", limit=2)
    assert len(hits) <= 2
    scores = [index.score("high cpu saturation throttling", chunk, 0) for chunk in hits]
    assert scores == sorted(scores, reverse=True)


def test_an_uninformative_query_returns_nothing() -> None:
    index = load_runbooks(RUNBOOK_DIR)
    assert index.search("", limit=3) == []
    assert index.search("and the of a to", limit=3) == []
