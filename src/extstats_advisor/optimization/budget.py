"""Immutable optimizer resource budget contract."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from extstats_advisor.errors import OptimizationPlanningError

OPTIMIZATION_BUDGET_CONTRACT = "optimization-budget-v1"
DEFAULT_WALL_CLOCK_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class OptimizationBudget:
    """Budget reserved for a future configuration-search execution phase."""

    candidate_limit: int
    wall_clock_seconds: float = DEFAULT_WALL_CLOCK_SECONDS
    max_statistics_count: int | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.candidate_limit, bool)
            or not isinstance(self.candidate_limit, int)
            or self.candidate_limit < 1
        ):
            raise OptimizationPlanningError("candidate_limit must be an integer >= 1")
        if isinstance(self.wall_clock_seconds, bool) or not isinstance(
            self.wall_clock_seconds, (int, float)
        ):
            raise OptimizationPlanningError("wall_clock_seconds must be finite and positive")
        wall_clock_seconds = float(self.wall_clock_seconds)
        if not math.isfinite(wall_clock_seconds) or wall_clock_seconds <= 0:
            raise OptimizationPlanningError("wall_clock_seconds must be finite and positive")
        object.__setattr__(self, "wall_clock_seconds", wall_clock_seconds)
        if self.max_statistics_count is not None and (
            isinstance(self.max_statistics_count, bool)
            or not isinstance(self.max_statistics_count, int)
            or self.max_statistics_count < 1
            or self.max_statistics_count > self.candidate_limit
        ):
            raise OptimizationPlanningError(
                "max_statistics_count must be an integer in [1, candidate_limit]"
            )

    @property
    def contract(self) -> str:
        return OPTIMIZATION_BUDGET_CONTRACT

    def to_dict(self) -> dict[str, Any]:
        value = {
            "contract": OPTIMIZATION_BUDGET_CONTRACT,
            "candidate_limit": self.candidate_limit,
            "wall_clock_seconds": self.wall_clock_seconds,
        }
        if self.max_statistics_count is not None:
            value["max_statistics_count"] = self.max_statistics_count
        return value

    @property
    def effective_max_statistics_count(self) -> int:
        """Return B, with the v1 compatibility default B == candidate_limit."""

        return (
            self.candidate_limit if self.max_statistics_count is None else self.max_statistics_count
        )
