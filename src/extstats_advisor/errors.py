"""Explicit exception boundaries for the production advisor."""


class ExtStatsAdvisorError(Exception):
    """Base class for expected advisor errors."""


class SnapshotValidationError(ExtStatsAdvisorError):
    """Raised when a snapshot is malformed, inconsistent, or tampered with."""


class SnapshotCompatibilityError(SnapshotValidationError):
    """Raised when a snapshot cannot be consumed by this implementation."""


class CandidateGenerationError(ExtStatsAdvisorError):
    """Raised when workload-derived candidate generation cannot proceed safely."""


class CandidateUniverseValidationError(CandidateGenerationError):
    """Raised when a candidate-universe artifact is malformed or inconsistent."""


class NativeStatsMaterializationError(ExtStatsAdvisorError):
    """Raised when patched PostgreSQL cannot materialize native statistics safely."""


class NativeStatsRepositoryValidationError(NativeStatsMaterializationError):
    """Raised when a native-statistics repository is malformed or inconsistent."""


class PlannerSandboxError(ExtStatsAdvisorError):
    """Raised when a PostgreSQL planner sandbox cannot be used safely."""


class PlannerSandboxValidationError(PlannerSandboxError):
    """Raised when live sandbox identity or frozen statistics have drifted."""


class PlannerQueryError(PlannerSandboxError):
    """Raised when a workload query is outside the planner sandbox scope."""


class GroundTruthError(ExtStatsAdvisorError):
    """Raised when exact-cardinality truth cannot be safely used."""


class GroundTruthValidationError(GroundTruthError):
    """Raised when a GroundTruthSet artifact is malformed or incompatible."""


class GroundTruthAcquisitionError(GroundTruthError):
    """Raised when production exact-cardinality collection fails."""


class UtilityError(ExtStatsAdvisorError):
    """Raised when a loss or workload utility cannot be evaluated."""


class UtilityValidationError(UtilityError):
    """Raised when estimates, truth, or utility inputs are invalid."""


class SingletonProfilingError(ExtStatsAdvisorError):
    """Raised when singleton utility profiling cannot complete safely."""


class SingletonProfileValidationError(SingletonProfilingError):
    """Raised when a SingletonProfile artifact is malformed or incompatible."""
