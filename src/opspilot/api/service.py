"""Assembly of one investigation request: provider, telemetry, tools, runtime, store.

Everything the API needs to run an agent is built here, from settings, once per request. It exists as a
module rather than inside the route so that the route is about HTTP and this is about wiring — and so a
test can substitute any single piece without standing up a server.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from opspilot.agent.runtime import AgentRuntime
from opspilot.agent.store import PostgresRunStore
from opspilot.agent.types import AgentStep, RunLimits
from opspilot.agents.opspilot.pipeline import investigation_steps
from opspilot.config import Settings
from opspilot.gateway.accounting import PostgresLedger, PostgresRecorder
from opspilot.gateway.gateway import ModelGateway, gateway_from_settings
from opspilot.gateway.providers.base import ModelProvider
from opspilot.gateway.providers.fake import FakeProvider
from opspilot.gateway.providers.litellm_provider import LiteLLMProvider
from opspilot.lab.control import LabControlClient
from opspilot.observability.logging import get_logger
from opspilot.observability.tracing import OtelStepTracer
from opspilot.retrieval.runbooks import RunbookIndex, load_runbooks
from opspilot.telemetry.http_source import HttpTelemetrySource
from opspilot.telemetry.source import TelemetrySource
from opspilot.telemetry.types import SourceLabel
from opspilot.tools.action_tools import ActionLog, action_tools
from opspilot.tools.audit import PostgresAuditRecorder
from opspilot.tools.executor import ToolExecutor
from opspilot.tools.registry import ToolRegistry
from opspilot.tools.runbook_tools import runbook_tools
from opspilot.tools.telemetry_tools import telemetry_tools

logger = get_logger(__name__)

RUNBOOK_DIR = "runbooks"

#: Which provider labels the platform will run. ``configured`` uses whatever the model policy names;
#: ``reference``/``fake`` are deterministic and free, which is what a public demo should default to
#: unless someone has explicitly decided to pay for clicks.
PROVIDER_LABELS = ("configured", "fake")


@dataclass
class Investigation:
    """A ready-to-execute investigation, with the pieces the caller may want to report on."""

    runtime: AgentRuntime
    actions: ActionLog
    store: PostgresRunStore
    source: TelemetrySource
    runbooks: RunbookIndex
    provider_label: str
    model: str
    gateway: ModelGateway
    executor: ToolExecutor
    registry: ToolRegistry

    def steps(self) -> list[AgentStep]:
        return investigation_steps(gateway=self.gateway, executor=self.executor, runbooks=self.runbooks)


#: The one module-level seam in this file: an in-process transport for the Incident Lab, set by tests so
#: the API can be exercised without a second process. Threading a transport through every call site would
#: make production code carry a test concern.
LAB_TRANSPORT: httpx.AsyncBaseTransport | None = None


def lab_control(settings: Settings) -> LabControlClient:
    """Admin access to the lab, for arming a scenario before investigating it."""
    return LabControlClient(settings.lab_base_url, token=settings.lab_admin_token, transport=LAB_TRANSPORT)


def provider_for(label: str, settings: Settings) -> tuple[ModelProvider, str]:
    """Resolve a provider label to a provider and the model name it will be asked for.

    ``configured`` needs a key; rather than fail at the first call, it falls back to the deterministic
    provider and says so, because a demo that errors on a missing secret teaches a visitor nothing.
    """
    if label == "configured" and not has_provider_key(settings):
        logger.warning("api.provider_fallback", requested="configured", reason="no provider key")
        return FakeProvider(), "fake/fake-1"
    if label == "configured":
        return LiteLLMProvider(), settings.default_model
    return FakeProvider(), "fake/fake-1"


def has_provider_key(settings: Settings) -> bool:
    import os

    from opspilot.evals.preflight import key_status

    return key_status(settings.default_model, environ=os.environ).ready


def telemetry_for(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> TelemetrySource:
    """Telemetry for a run: the incident lab, labelled ``demo`` until a live source produces it."""
    label: SourceLabel = "demo"
    return HttpTelemetrySource(settings.lab_base_url, label=label, transport=transport)


def build_investigation(
    session: AsyncSession,
    settings: Settings,
    *,
    provider_label: str = "configured",
    transport: httpx.AsyncBaseTransport | None = None,
    runbooks: RunbookIndex | None = None,
    with_tracing: bool = True,
) -> Investigation:
    provider, model = provider_for(provider_label, settings)
    recorder = PostgresRecorder(session)
    ledger = PostgresLedger(session)
    gateway: ModelGateway = gateway_from_settings(
        settings, providers=[provider], recorder=recorder, ledger=ledger, model=model
    )
    source = telemetry_for(settings, transport if transport is not None else LAB_TRANSPORT)
    actions = ActionLog()
    index = runbooks or load_runbooks(RUNBOOK_DIR)
    registry = ToolRegistry(telemetry_tools(source) + runbook_tools(index) + action_tools(actions))
    store = PostgresRunStore(session)
    runtime = AgentRuntime(
        store=store,
        ledger=ledger,
        limits=RunLimits(
            max_steps=settings.run_max_steps,
            timeout_seconds=settings.run_timeout_seconds,
            cost_cap_eur=settings.run_cost_cap_eur,
        ),
        tracer=OtelStepTracer() if with_tracing else None,
    )
    return Investigation(
        runtime=runtime,
        actions=actions,
        store=store,
        source=source,
        runbooks=index,
        provider_label=provider_label,
        model=model,
        gateway=gateway,
        executor=ToolExecutor(registry=registry, audit=PostgresAuditRecorder(session)),
        registry=registry,
    )
