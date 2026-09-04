"""Deterministic chaos/property-testing lab for HelixGrid scheduling invariants.

The package contains an independent reference model rather than importing the Go
coordinator. That separation is intentional: a model that shares implementation code
with the system under test tends to reproduce the same bugs instead of detecting them.
"""

from .model import (
    Action,
    ActionKind,
    Event,
    InvariantViolation,
    Lease,
    Model,
    RetryPolicy,
    Task,
    TaskState,
    Worker,
    Workflow,
    WorkflowState,
)

__all__ = [
    "Action",
    "ActionKind",
    "Event",
    "InvariantViolation",
    "Lease",
    "Model",
    "RetryPolicy",
    "Task",
    "TaskState",
    "Worker",
    "Workflow",
    "WorkflowState",
]

from .generator import GeneratedScenario, GeneratedWorkflow, GeneratorConfig, ScenarioGenerator
from .runner import CampaignReport, ReplayFailure, ReplayReport, ScenarioRunner, run_campaign
from .shrinker import ScenarioShrinker, ShrinkResult

__all__ += [
    "GeneratedScenario",
    "GeneratedWorkflow",
    "GeneratorConfig",
    "ScenarioGenerator",
    "CampaignReport",
    "ReplayFailure",
    "ReplayReport",
    "ScenarioRunner",
    "run_campaign",
    "ScenarioShrinker",
    "ShrinkResult",
]
