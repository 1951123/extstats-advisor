"""Replaceable workload utility providers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Protocol

from extstats_advisor.errors import UtilityValidationError
from extstats_advisor.ground_truth.provider import GroundTruthProvider
from extstats_advisor.snapshot.model import Workload
from extstats_advisor.utility.loss import CardinalityLoss
from extstats_advisor.utility.model import PerQueryUtility, UtilityResult


class UtilityProvider(Protocol):
    def evaluate(self, estimates: Mapping[str, float]) -> UtilityResult:
        """Evaluate one manually supplied planner configuration."""


class WeightedWorkloadUtility:
    """Weighted arithmetic mean of one loss across positive-weight workload queries."""

    def __init__(
        self,
        workload: Workload,
        ground_truth: GroundTruthProvider,
        loss: CardinalityLoss,
    ) -> None:
        self._workload = workload
        self._ground_truth = ground_truth
        self._loss = loss
        contract = getattr(loss, "contract_version", None)
        if not isinstance(contract, str) or not contract:
            raise UtilityValidationError("cardinality loss contract_version is required")

    def evaluate(self, estimates: Mapping[str, float]) -> UtilityResult:
        if not isinstance(estimates, Mapping):
            raise UtilityValidationError("planner estimates must be a mapping")
        query_ids = {query.query_id for query in self._workload.queries}
        unknown = set(estimates) - query_ids
        if unknown:
            raise UtilityValidationError(
                f"planner estimates contain unknown query IDs: {sorted(unknown)}"
            )
        records = []
        total_weight = 0.0
        weighted_loss = 0.0
        for query in self._workload.queries:
            if query.weight <= 0:
                continue
            if query.query_id not in estimates:
                raise UtilityValidationError(
                    f"planner estimate is missing positive-weight query: {query.query_id}"
                )
            truth = self._ground_truth.truth_for(query.query_id)
            if truth.query_id != query.query_id:
                raise UtilityValidationError("ground-truth provider returned the wrong query")
            estimate = estimates[query.query_id]
            loss = self._loss.loss(estimate, truth.cardinality)
            estimate_value = float(estimate)
            if not math.isfinite(estimate_value):
                raise UtilityValidationError("planner estimate must be finite")
            records.append(
                PerQueryUtility(
                    query.query_id, float(query.weight), estimate_value, truth.cardinality, loss
                )
            )
            total_weight += float(query.weight)
            weighted_loss += float(query.weight) * loss
        if not records or total_weight <= 0:
            raise UtilityValidationError("workload has no positive-weight utility queries")
        return UtilityResult(
            weighted_loss / total_weight,
            self._loss.contract_version,
            len(records),
            total_weight,
            tuple(records),
        )
