"""Portable predicate profiles emitted by DBMS-specific workload analyzers."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from extstats_advisor.errors import CandidateGenerationError

ANALYSIS_CONTRACT_VERSION = "postgresql-simple-selection-v1"
SUPPORTED_ANALYSIS_STATUS = "supported"
UNSUPPORTED_ANALYSIS_STATUS = "unsupported"


@dataclass(frozen=True, slots=True)
class PredicateProfile:
    """A portable summary of predicate columns for one workload query."""

    query_id: str
    relation_id: str | None
    weight: float
    predicate_column_ordinals: tuple[int, ...]
    predicate_column_names: tuple[str, ...]
    analysis_status: str
    analysis_contract_version: str = ANALYSIS_CONTRACT_VERSION
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.query_id, str) or not self.query_id.strip():
            raise CandidateGenerationError("predicate profile query_id must be non-empty")
        if self.relation_id is not None and (
            not isinstance(self.relation_id, str) or not self.relation_id.strip()
        ):
            raise CandidateGenerationError("predicate profile relation_id must be non-empty")
        if (
            not isinstance(self.weight, (int, float))
            or isinstance(self.weight, bool)
            or not math.isfinite(float(self.weight))
        ):
            raise CandidateGenerationError(f"invalid workload weight for query {self.query_id}")
        if self.weight < 0:
            raise CandidateGenerationError(f"negative workload weight for query {self.query_id}")
        if self.analysis_status not in {SUPPORTED_ANALYSIS_STATUS, UNSUPPORTED_ANALYSIS_STATUS}:
            raise CandidateGenerationError(f"unknown analysis status for query {self.query_id}")
        if self.analysis_contract_version != ANALYSIS_CONTRACT_VERSION:
            raise CandidateGenerationError("unsupported predicate analysis contract version")
        if len(self.predicate_column_ordinals) != len(self.predicate_column_names):
            raise CandidateGenerationError(f"profile column mismatch for query {self.query_id}")
        if tuple(sorted(set(self.predicate_column_ordinals))) != self.predicate_column_ordinals:
            raise CandidateGenerationError(
                f"profile ordinals are not canonical for query {self.query_id}"
            )
        if any(not isinstance(item, int) or item < 1 for item in self.predicate_column_ordinals):
            raise CandidateGenerationError(f"invalid profile ordinal for query {self.query_id}")
        if any(not isinstance(item, str) or not item for item in self.predicate_column_names):
            raise CandidateGenerationError(f"invalid profile column name for query {self.query_id}")
        if self.analysis_status == SUPPORTED_ANALYSIS_STATUS and self.reason is not None:
            raise CandidateGenerationError(f"supported query {self.query_id} cannot have a reason")
        if self.analysis_status == UNSUPPORTED_ANALYSIS_STATUS and (
            not isinstance(self.reason, str) or not self.reason.strip()
        ):
            raise CandidateGenerationError(f"unsupported query {self.query_id} requires a reason")

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "query_id": self.query_id,
            "relation_id": self.relation_id,
            "weight": self.weight,
            "predicate_column_ordinals": list(self.predicate_column_ordinals),
            "predicate_column_names": list(self.predicate_column_names),
            "analysis_status": self.analysis_status,
            "analysis_contract_version": self.analysis_contract_version,
        }
        if self.reason is not None:
            value["reason"] = self.reason
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PredicateProfile:
        try:
            return cls(
                query_id=value["query_id"],
                relation_id=value.get("relation_id"),
                weight=value["weight"],
                predicate_column_ordinals=tuple(value["predicate_column_ordinals"]),
                predicate_column_names=tuple(value["predicate_column_names"]),
                analysis_status=value["analysis_status"],
                analysis_contract_version=value.get(
                    "analysis_contract_version", ANALYSIS_CONTRACT_VERSION
                ),
                reason=value.get("reason"),
            )
        except (KeyError, TypeError) as exc:
            raise CandidateGenerationError("invalid predicate profile") from exc
