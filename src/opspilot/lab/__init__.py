"""The Incident Lab: a demo service with injectable faults and withheld ground truth."""

from opspilot.lab.faults import DeploymentChange, FaultConfig, FaultKind, LabState
from opspilot.lab.service import create_lab_app

__all__ = [
    "DeploymentChange",
    "FaultConfig",
    "FaultKind",
    "LabState",
    "create_lab_app",
]
