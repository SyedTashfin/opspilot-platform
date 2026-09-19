"""Telemetry access: metrics, logs, deployments and resource state.

Everything a tool returns from here carries a ``source`` label (``demo`` or ``live``) so that no
observation can reach the UI or an evaluation report without saying where it came from.
"""

from opspilot.telemetry.scenarios import SCENARIOS, IncidentScenario, get_scenario
from opspilot.telemetry.source import TelemetrySource
from opspilot.telemetry.synthetic import SyntheticTelemetrySource
from opspilot.telemetry.types import (
    Deployment,
    LogLine,
    MetricSeries,
    MetricsSnapshot,
    ResourceState,
)

__all__ = [
    "SCENARIOS",
    "Deployment",
    "IncidentScenario",
    "LogLine",
    "MetricSeries",
    "MetricsSnapshot",
    "ResourceState",
    "SyntheticTelemetrySource",
    "TelemetrySource",
    "get_scenario",
]
