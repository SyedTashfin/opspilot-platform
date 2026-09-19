"""Retrieval over operational runbooks.

Lexical retrieval ships first, on purpose: the corpus is a few dozen documents, and the honest
question — "do embeddings earn their complexity here?" — can only be answered by the evaluation
suite, not by taste. If the benchmark in M7 shows a grounding gap that embeddings close, pgvector
arrives with evidence attached.
"""

from opspilot.retrieval.runbooks import RunbookChunk, RunbookIndex, load_runbooks

__all__ = ["RunbookChunk", "RunbookIndex", "load_runbooks"]
