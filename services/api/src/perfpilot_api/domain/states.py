from enum import StrEnum


class AnalysisState(StrEnum):
    CREATING = "creating"
    CREATED = "created"
    UPLOADING = "uploading"
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    PARTIALLY_COMPLETED = "partially_completed"
    FAILED = "failed"
    CANCELED = "canceled"
    DELETED = "deleted"


class ScenarioState(StrEnum):
    QUEUED = "queued"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    ANALYZING = "analyzing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


ANALYSIS_TERMINAL_STATES = frozenset(
    {
        AnalysisState.COMPLETED,
        AnalysisState.PARTIALLY_COMPLETED,
        AnalysisState.FAILED,
        AnalysisState.CANCELED,
        AnalysisState.DELETED,
    }
)

SCENARIO_TERMINAL_STATES = frozenset(
    {
        ScenarioState.COMPLETED,
        ScenarioState.FAILED,
        ScenarioState.CANCELED,
    }
)
