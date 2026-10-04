"""Immutable diagnostic results for workload utility evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from extstats_advisor.errors import UtilityValidationError


@dataclass(frozen=True, slots=True)
class PerQueryUtility:
    query_id: str
    weight: float
    estimate: float
    truth: int
    loss: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "weight": self.weight,
            "estimate": self.estimate,
            "truth": self.truth,
            "loss": self.loss,
        }


@dataclass(frozen=True, slots=True)
class UtilityResult:
    objective: float
    loss_contract: str
    query_count: int
    total_weight: float
    per_query: tuple[PerQueryUtility, ...]

    def __post_init__(self) -> None:
        if not math.isfinite(self.objective) or self.objective < 0:
            raise UtilityValidationError("utility objective must be finite and non-negative")
        if not isinstance(self.loss_contract, str) or not self.loss_contract:
            raise UtilityValidationError("utility loss contract is required")
        if self.query_count != len(self.per_query) or self.query_count < 1:
            raise UtilityValidationError("utility query_count is inconsistent")
        if not math.isfinite(self.total_weight) or self.total_weight <= 0:
            raise UtilityValidationError("utility total_weight must be positive and finite")

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "loss_contract": self.loss_contract,
            "query_count": self.query_count,
            "total_weight": self.total_weight,
            "per_query": [item.to_dict() for item in self.per_query],
        }
