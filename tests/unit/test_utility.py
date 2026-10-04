from __future__ import annotations

import math

import pytest

from extstats_advisor.errors import GroundTruthError, UtilityValidationError
from extstats_advisor.ground_truth import (
    PRODUCTION_EXACT_SOURCE,
    ArtifactGroundTruthProvider,
    CardinalityTruth,
    GroundTruthSet,
    GroundTruthSource,
)
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from extstats_advisor.utility import QErrorLoss, WeightedWorkloadUtility


def _provider() -> ArtifactGroundTruthProvider:
    return ArtifactGroundTruthProvider(
        GroundTruthSet(
            "a" * 64,
            "w",
            GroundTruthSource(PRODUCTION_EXACT_SOURCE, "postgresql", "16.14", 160014, "1:2:"),
            (
                CardinalityTruth("q1", 100, PRODUCTION_EXACT_SOURCE),
                CardinalityTruth("q2", 0, PRODUCTION_EXACT_SOURCE),
            ),
        )
    )


def _workload() -> Workload:
    return Workload(
        "w",
        (
            WorkloadQuery("q1", "SELECT 1", 2),
            WorkloadQuery("q2", "SELECT 2", 1),
            WorkloadQuery("q_zero", "SELECT 3", 0),
        ),
    )


@pytest.mark.parametrize(
    ("estimate", "truth", "expected"),
    [
        (100, 100, 1),
        (50, 100, 2),
        (200, 100, 2),
        (0, 0, 1),
        (1, 0, 1),
        (10, 0, 10),
        (0, 1000, 1000),
        (500.5, 1000, 1000 / 500.5),
    ],
)
def test_qerror_floor_one(estimate, truth, expected) -> None:
    assert QErrorLoss().loss(estimate, truth) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("estimate", "truth"),
    [(-1, 1), (math.inf, 1), (math.nan, 1), (1, -1), (1, True)],
)
def test_qerror_rejects_invalid_values(estimate, truth) -> None:
    with pytest.raises(UtilityValidationError):
        QErrorLoss().loss(estimate, truth)


def test_weighted_utility_ignores_zero_weight_and_returns_deterministic_records() -> None:
    result = WeightedWorkloadUtility(_workload(), _provider(), QErrorLoss()).evaluate(
        {"q1": 50, "q2": 0}
    )
    assert result.objective == pytest.approx(5 / 3)
    assert result.loss_contract == "qerror-cardinality-floor-1-v1"
    assert result.query_count == 2
    assert result.total_weight == 3
    assert [item.query_id for item in result.per_query] == ["q1", "q2"]
    assert result.per_query[0].truth == 100


def test_weighted_utility_rejects_missing_unknown_and_missing_truth() -> None:
    utility = WeightedWorkloadUtility(_workload(), _provider(), QErrorLoss())
    with pytest.raises(UtilityValidationError, match="missing"):
        utility.evaluate({})
    with pytest.raises(UtilityValidationError, match="unknown"):
        utility.evaluate({"q1": 100, "extra": 1})
    missing_truth = ArtifactGroundTruthProvider(
        GroundTruthSet(
            "a" * 64,
            "w",
            GroundTruthSource(PRODUCTION_EXACT_SOURCE, "postgresql", "16.14", 160014, "1:2:"),
            (CardinalityTruth("q1", 100, PRODUCTION_EXACT_SOURCE),),
        )
    )
    with pytest.raises(GroundTruthError, match="missing query"):
        WeightedWorkloadUtility(_workload(), missing_truth, QErrorLoss()).evaluate(
            {"q1": 100, "q2": 0}
        )
