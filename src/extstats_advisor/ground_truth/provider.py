"""Replaceable providers for immutable cardinality truth."""

from __future__ import annotations

from typing import Protocol

from extstats_advisor.errors import GroundTruthError
from extstats_advisor.ground_truth.model import CardinalityTruth, GroundTruthSet


class GroundTruthProvider(Protocol):
    def truth_for(self, query_id: str) -> CardinalityTruth:
        """Return exact or otherwise authoritative truth for one query."""


class ArtifactGroundTruthProvider:
    """GroundTruthProvider backed by a validated standalone GroundTruthSet."""

    def __init__(self, ground_truth: GroundTruthSet) -> None:
        self._truths = {truth.query_id: truth for truth in ground_truth.truths}

    def truth_for(self, query_id: str) -> CardinalityTruth:
        try:
            return self._truths[query_id]
        except KeyError as exc:
            raise GroundTruthError(f"ground truth is missing query: {query_id}") from exc


class ProductionExactCardinalityProvider(ArtifactGroundTruthProvider):
    """The v1 production reference provider, currently artifact-backed."""
