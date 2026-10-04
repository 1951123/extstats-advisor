"""Optimization planning contracts that precede future configuration search."""

from extstats_advisor.optimization.artifact import (
    inspect_singleton_profile,
    load_singleton_profile,
    validate_singleton_profile,
    write_singleton_profile,
)
from extstats_advisor.optimization.budget import (
    DEFAULT_WALL_CLOCK_SECONDS,
    OPTIMIZATION_BUDGET_CONTRACT,
    OptimizationBudget,
)
from extstats_advisor.optimization.plan import (
    OPTIMIZATION_PLAN_FORMAT_VERSION,
    SCREENING_POLICY,
    OptimizationPlan,
    ScreenedCandidate,
    create_optimization_plan,
)
from extstats_advisor.optimization.plan_artifact import (
    inspect_optimization_plan,
    load_optimization_plan,
    validate_optimization_plan,
    write_optimization_plan,
)
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    SINGLETON_PROFILE_FORMAT_VERSION,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
    profile_singletons,
)

__all__ = [
    "DEFAULT_WALL_CLOCK_SECONDS",
    "OPTIMIZATION_BUDGET_CONTRACT",
    "OPTIMIZATION_PLAN_FORMAT_VERSION",
    "SCREENING_POLICY",
    "SINGLETON_PRECEDENCE_POLICY",
    "SINGLETON_PROFILE_FORMAT_VERSION",
    "BaselineProfile",
    "CandidateSingletonProfile",
    "OptimizationBudget",
    "OptimizationPlan",
    "ScreenedCandidate",
    "SingletonProfile",
    "create_optimization_plan",
    "inspect_optimization_plan",
    "inspect_singleton_profile",
    "load_optimization_plan",
    "load_singleton_profile",
    "profile_singletons",
    "validate_optimization_plan",
    "validate_singleton_profile",
    "write_optimization_plan",
    "write_singleton_profile",
]
