"""DBMS-neutral cardinality loss functions."""

from __future__ import annotations

import math
from numbers import Real
from typing import Protocol

from extstats_advisor.errors import UtilityValidationError

QERROR_CONTRACT_VERSION = "qerror-cardinality-floor-1-v1"


class CardinalityLoss(Protocol):
    contract_version: str

    def loss(self, estimate: float, truth: int) -> float:
        """Return a non-negative loss for one estimate/truth pair."""


class QErrorLoss:
    """Symmetric q-error with an explicit floor of one for zero values."""

    contract_version = QERROR_CONTRACT_VERSION

    def loss(self, estimate: float, truth: int) -> float:
        if isinstance(estimate, bool) or not isinstance(estimate, Real):
            raise UtilityValidationError("cardinality estimate must be numeric")
        try:
            estimate_value = float(estimate)
        except (TypeError, ValueError) as exc:
            raise UtilityValidationError("cardinality estimate must be numeric") from exc
        if not math.isfinite(estimate_value) or estimate_value < 0:
            raise UtilityValidationError("cardinality estimate must be finite and non-negative")
        if not isinstance(truth, int) or isinstance(truth, bool) or truth < 0:
            raise UtilityValidationError("cardinality truth must be a non-negative integer")
        estimate_floor = max(estimate_value, 1.0)
        truth_floor = max(truth, 1)
        return max(estimate_floor / truth_floor, truth_floor / estimate_floor)
