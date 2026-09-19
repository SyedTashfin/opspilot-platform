"""Read-only tool factories over a telemetry source.

Every argument model is the schema an LLM sees, and every result model is what the agent reasons
over
and what the run record stores. Both are strict: a tool that silently accepts a misspelled filter is
a tool that quietly returns the wrong evidence.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from opspilot.domain.enums import PermissionClass, RiskLevel
from opspilot.telemetry.source import TelemetrySource
from opspilot.tools.types import ToolContext, ToolDefinition


class LogQueryArgs(BaseModel):
    service: str = Field(description="Service name, for example recommendation-service")
    window_minutes: int = Field(default=30, ge=1, le=180, description="Lookback window")
    level: Literal["INFO", "WARN", "ERROR"] | None = Field(
        default=None, description="Filter by level"
    )
    contains: str | None = Field(default=None, description="Substring match, case-insensitive")
    limit: int = Field(default=50, ge=1, le=200)


class LogLineModel(BaseModel):
    at: datetime
    level: str
    message: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class LogQueryResult(BaseModel):
    service: str
    window_minutes: int
    lines: list[LogLineModel]
    source: str


class MetricsArgs(BaseModel):
    service: str
    window_minutes: int = Field(default=30, ge=1, le=180)
    metrics: list[str] | None = Field(
        default=None, description="Optional subset of metric names to return"
    )


class MetricSummary(BaseModel):
    unit: str
    latest: float
    max: float
    p95: float


class MetricsResult(BaseModel):
    service: str
    window_minutes: int
    series: dict[str, MetricSummary]
    source: str


class DeploymentsArgs(BaseModel):
    service: str
    limit: int = Field(default=5, ge=1, le=20)


class DeploymentModel(BaseModel):
    version: str
    deployed_at: datetime
    author: str
    changes: list[str]


class DeploymentsResult(BaseModel):
    service: str
    deployments: list[DeploymentModel]
    source: str


class ResourceStateArgs(BaseModel):
    service: str


class ResourceStateResult(BaseModel):
    service: str
    replicas: int
    desired_replicas: int
    cpu_limit: str
    memory_limit: str
    restarts_last_hour: int
    source: str


def telemetry_tools(source: TelemetrySource) -> list[ToolDefinition]:
    async def get_metrics(arguments: MetricsArgs, context: ToolContext) -> MetricsResult:
        snapshot = await source.metrics(arguments.service, arguments.window_minutes)
        observation = snapshot.as_observation()
        series: dict[str, Any] = observation["series"]
        if arguments.metrics:
            wanted = set(arguments.metrics)
            series = {name: value for name, value in series.items() if name in wanted}
        return MetricsResult(
            service=snapshot.service,
            window_minutes=snapshot.window_minutes,
            series={name: MetricSummary(**value) for name, value in series.items()},
            source=snapshot.source,
        )

    async def query_logs(arguments: LogQueryArgs, context: ToolContext) -> LogQueryResult:
        lines = await source.logs(
            arguments.service,
            arguments.window_minutes,
            level=arguments.level,
            contains=arguments.contains,
            limit=arguments.limit,
        )
        return LogQueryResult(
            service=arguments.service,
            window_minutes=arguments.window_minutes,
            lines=[
                LogLineModel(
                    at=line.at, level=line.level, message=line.message, attributes=line.attributes
                )
                for line in lines
            ],
            source=source.label,
        )

    async def get_recent_deployments(
        arguments: DeploymentsArgs, context: ToolContext
    ) -> DeploymentsResult:
        deployments = await source.deployments(arguments.service, limit=arguments.limit)
        return DeploymentsResult(
            service=arguments.service,
            deployments=[
                DeploymentModel(
                    version=deployment.version,
                    deployed_at=deployment.deployed_at,
                    author=deployment.author,
                    changes=list(deployment.changes),
                )
                for deployment in deployments
            ],
            source=source.label,
        )

    async def get_resource_state(
        arguments: ResourceStateArgs, context: ToolContext
    ) -> ResourceStateResult:
        state = await source.resource_state(arguments.service)
        return ResourceStateResult(
            service=state.service,
            replicas=state.replicas,
            desired_replicas=state.desired_replicas,
            cpu_limit=state.cpu_limit,
            memory_limit=state.memory_limit,
            restarts_last_hour=state.restarts_last_hour,
            source=state.source,
        )

    return [
        ToolDefinition(
            name="azure.get_metrics",
            description="Read metric series for a service over a lookback window.",
            handler=get_metrics,
            input_model=MetricsArgs,
            output_model=MetricsResult,
            permission_class=PermissionClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            timeout_seconds=15.0,
        ),
        ToolDefinition(
            name="azure.query_logs",
            description=(
                "Search a service's logs over a lookback window, optionally by level or text."
            ),
            handler=query_logs,
            input_model=LogQueryArgs,
            output_model=LogQueryResult,
            permission_class=PermissionClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            timeout_seconds=15.0,
        ),
        ToolDefinition(
            name="github.get_recent_deployments",
            description="List recent deployments for a service with their change summaries.",
            handler=get_recent_deployments,
            input_model=DeploymentsArgs,
            output_model=DeploymentsResult,
            permission_class=PermissionClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            timeout_seconds=15.0,
        ),
        ToolDefinition(
            name="azure.get_resource_state",
            description="Read replica counts, limits and restart counts for a service.",
            handler=get_resource_state,
            input_model=ResourceStateArgs,
            output_model=ResourceStateResult,
            permission_class=PermissionClass.READ_ONLY,
            risk_level=RiskLevel.LOW,
            timeout_seconds=15.0,
        ),
    ]
