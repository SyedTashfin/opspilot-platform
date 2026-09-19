"""Runbook retrieval tool.

The tool returns chunks with citation ids and content hashes, so anything the agent asserts from a
runbook can be traced to a specific chunk of a specific file. Retrieved text is data: it is
passed to
the model as evidence, never as instructions (see ADR-016 and the injection tests in M8).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from opspilot.domain.enums import PermissionClass, RiskLevel
from opspilot.retrieval.runbooks import RunbookIndex
from opspilot.tools.types import ToolContext, ToolDefinition


class RunbookSearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=300, description="Symptom or cause to look up")
    limit: int = Field(default=3, ge=1, le=5)


class RunbookChunkModel(BaseModel):
    citation_id: str
    document: str
    heading: str
    content_hash: str
    excerpt: str


class RunbookSearchResult(BaseModel):
    query: str
    chunks: list[RunbookChunkModel]
    indexed_chunks: int


def runbook_tools(index: RunbookIndex) -> list[ToolDefinition]:
    async def search_runbook(
        arguments: RunbookSearchArgs, context: ToolContext
    ) -> RunbookSearchResult:
        chunks = index.search(arguments.query, limit=arguments.limit)
        return RunbookSearchResult(
            query=arguments.query,
            chunks=[RunbookChunkModel(**chunk.as_evidence()) for chunk in chunks],
            indexed_chunks=len(index),
        )

    return [
        ToolDefinition(
            name="docs.search_runbook",
            description="Search operational runbooks and return cited chunks.",
            handler=search_runbook,
            input_model=RunbookSearchArgs,
            output_model=RunbookSearchResult,
            permission_class=PermissionClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            timeout_seconds=10.0,
        )
    ]
