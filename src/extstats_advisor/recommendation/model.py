"""Immutable, DBMS-neutral deployment recommendation models."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from extstats_advisor.errors import RecommendationValidationError

RECOMMENDATION_FORMAT_VERSION = "statistics-recommendation-v1"
POSTGRES_DEPLOYMENT_CONTRACT = "postgresql-extended-statistics-deployment-v1"
POSTGRES_NAMING_POLICY = "postgresql-advisor-statistics-name-v1"
POSTGRES_PHYSICAL_ORDER_CONTRACT = "postgresql-statistics-oid-order-v1"
PRECEDENCE_SOURCE = "singleton-profile"
PRECEDENCE_POLICY = "singleton-utility-precedence-v1"
DECISION_PROPOSE_CHANGE = "propose-change"
DECISION_NO_CHANGE = "no-change"
POSTGRES_STATISTICS_KINDS = {
    "postgresql.mcv": "mcv",
    "postgresql.dependencies": "dependencies",
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SAFE_OBJECT_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise RecommendationValidationError(f"{label} must be a SHA-256 token")
    return value


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise RecommendationValidationError(f"{label} must be a non-empty string")
    return value


def _identifier(value: Any, label: str) -> str:
    _token(value, label)
    if len(value.encode("utf-8")) > 63:
        raise RecommendationValidationError(f"{label} exceeds PostgreSQL's 63-byte limit")
    return value


def _finite(value: Any, label: str, *, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecommendationValidationError(f"{label} must be finite")
    number = float(value)
    if not math.isfinite(number) or (nonnegative and number < 0):
        raise RecommendationValidationError(f"{label} must be finite")
    return number


@dataclass(frozen=True, slots=True)
class TargetRelation:
    relation_id: str
    catalog: str | None
    schema: str
    name: str

    def __post_init__(self) -> None:
        _token(self.relation_id, "target relation_id")
        if self.catalog is not None:
            _identifier(self.catalog, "target relation catalog")
        _identifier(self.schema, "target relation schema")
        _identifier(self.name, "target relation name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "catalog": self.catalog,
            "schema": self.schema,
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class StatisticsObject:
    schema: str
    name: str

    def __post_init__(self) -> None:
        _identifier(self.schema, "statistics object schema")
        _identifier(self.name, "statistics object name")
        if not _SAFE_OBJECT_NAME.fullmatch(self.name):
            raise RecommendationValidationError(
                "statistics object name is not a safe ASCII identifier"
            )

    def to_dict(self) -> dict[str, str]:
        return {"schema": self.schema, "name": self.name}


@dataclass(frozen=True, slots=True)
class SelectedCandidate:
    candidate_id: str
    kind: str
    relation_id: str
    column_ordinals: tuple[int, ...]
    column_names: tuple[str, ...]
    global_frozen_precedence_rank: int
    deployment_order_position: int
    statistics_object: StatisticsObject
    statistics_target: int

    def __post_init__(self) -> None:
        _token(self.candidate_id, "selected candidate_id")
        if self.kind not in POSTGRES_STATISTICS_KINDS:
            raise RecommendationValidationError("selected candidate kind is unsupported")
        _token(self.relation_id, "selected candidate relation_id")
        if (
            len(self.column_ordinals) != 2
            or tuple(sorted(self.column_ordinals)) != self.column_ordinals
        ):
            raise RecommendationValidationError("selected candidate columns are invalid")
        if len(self.column_names) != 2 or any(
            not isinstance(name, str) or not name for name in self.column_names
        ):
            raise RecommendationValidationError("selected candidate column names are invalid")
        for ordinal, name in zip(self.column_ordinals, self.column_names, strict=True):
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
                raise RecommendationValidationError("selected candidate column ordinal is invalid")
            _identifier(name, "selected candidate column name")
        for value, label in (
            (self.global_frozen_precedence_rank, "global frozen precedence rank"),
            (self.deployment_order_position, "deployment order position"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise RecommendationValidationError(f"{label} must be positive")
        if isinstance(self.statistics_target, bool) or not isinstance(self.statistics_target, int):
            raise RecommendationValidationError("statistics target must be an integer")
        if self.statistics_target < 1:
            raise RecommendationValidationError("statistics target must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "relation_id": self.relation_id,
            "column_ordinals": list(self.column_ordinals),
            "column_names": list(self.column_names),
            "global_frozen_precedence_rank": self.global_frozen_precedence_rank,
            "deployment_order_position": self.deployment_order_position,
            "statistics_object": self.statistics_object.to_dict(),
            "statistics_target": self.statistics_target,
        }


@dataclass(frozen=True, slots=True)
class CreateStatisticsAction:
    candidate_id: str
    relation_id: str
    object_schema: str
    object_name: str
    statistics_kind: str
    column_names: tuple[str, ...]

    action_type: str = field(default="create-statistics", init=False)

    def __post_init__(self) -> None:
        _token(self.candidate_id, "CREATE candidate_id")
        _token(self.relation_id, "CREATE relation_id")
        _identifier(self.object_schema, "CREATE object schema")
        _identifier(self.object_name, "CREATE object name")
        if self.statistics_kind not in POSTGRES_STATISTICS_KINDS:
            raise RecommendationValidationError("CREATE statistics kind is unsupported")
        if len(self.column_names) != 2:
            raise RecommendationValidationError("CREATE must contain two columns")
        for name in self.column_names:
            _identifier(name, "CREATE column name")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action_type,
            "candidate_id": self.candidate_id,
            "relation_id": self.relation_id,
            "object_schema": self.object_schema,
            "object_name": self.object_name,
            "statistics_kind": self.statistics_kind,
            "column_names": list(self.column_names),
        }


@dataclass(frozen=True, slots=True)
class SetStatisticsTargetAction:
    candidate_id: str
    object_schema: str
    object_name: str
    statistics_target: int

    action_type: str = field(default="set-statistics-target", init=False)

    def __post_init__(self) -> None:
        _token(self.candidate_id, "ALTER candidate_id")
        _identifier(self.object_schema, "ALTER object schema")
        _identifier(self.object_name, "ALTER object name")
        if isinstance(self.statistics_target, bool) or not isinstance(self.statistics_target, int):
            raise RecommendationValidationError("ALTER statistics target must be an integer")
        if self.statistics_target < 1:
            raise RecommendationValidationError("ALTER statistics target must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action_type,
            "candidate_id": self.candidate_id,
            "object_schema": self.object_schema,
            "object_name": self.object_name,
            "statistics_target": self.statistics_target,
        }


@dataclass(frozen=True, slots=True)
class AnalyzeRelationAction:
    relation: TargetRelation

    action_type: str = field(default="analyze-relation", init=False)

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action_type, "relation": self.relation.to_dict()}


DDLAction = CreateStatisticsAction | SetStatisticsTargetAction | AnalyzeRelationAction


@dataclass(frozen=True, slots=True)
class Recommendation:
    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    native_stats_repository_semantic_digest: str
    ground_truth_semantic_digest: str
    singleton_profile_semantic_digest: str
    optimization_plan_semantic_digest: str
    search_result_semantic_digest: str
    dbms_name: str
    dbms_source_version: str
    deployment_contract: str
    precedence_source: str
    precedence_policy: str
    naming_policy: str
    physical_order_contract: str
    search_policy: str
    search_termination_reason: str
    baseline_objective: float
    final_objective: float
    improvement: float
    decision: str
    target_relation: TargetRelation
    selected_candidate_ids: tuple[str, ...]
    deployment_ordered_candidate_ids: tuple[str, ...]
    selected_candidates: tuple[SelectedCandidate, ...]
    ddl_plan: tuple[DDLAction, ...]
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    created_at: str | None = field(default=None, repr=False, compare=False)
    semantic_digest: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("snapshot", self.source_snapshot_semantic_digest),
            ("candidate universe", self.candidate_universe_semantic_digest),
            ("native repository", self.native_stats_repository_semantic_digest),
            ("ground truth", self.ground_truth_semantic_digest),
            ("singleton profile", self.singleton_profile_semantic_digest),
            ("optimization plan", self.optimization_plan_semantic_digest),
            ("search result", self.search_result_semantic_digest),
        ):
            _digest(value, f"recommendation {label} digest")
        if self.dbms_name != "postgresql":
            raise RecommendationValidationError("recommendation DBMS must be postgresql")
        _token(self.dbms_source_version, "DBMS source version")
        if self.deployment_contract != POSTGRES_DEPLOYMENT_CONTRACT:
            raise RecommendationValidationError("unsupported deployment contract")
        if self.precedence_source != PRECEDENCE_SOURCE:
            raise RecommendationValidationError("unsupported precedence source")
        if self.precedence_policy != PRECEDENCE_POLICY:
            raise RecommendationValidationError("unsupported precedence policy")
        if self.naming_policy != POSTGRES_NAMING_POLICY:
            raise RecommendationValidationError("unsupported naming policy")
        if self.physical_order_contract != POSTGRES_PHYSICAL_ORDER_CONTRACT:
            raise RecommendationValidationError("unsupported physical order contract")
        _token(self.search_policy, "search policy")
        _token(self.search_termination_reason, "search termination reason")
        _finite(self.baseline_objective, "baseline objective", nonnegative=True)
        _finite(self.final_objective, "final objective", nonnegative=True)
        _finite(self.improvement, "recommendation improvement")
        if self.improvement != self.baseline_objective - self.final_objective:
            raise RecommendationValidationError("recommendation improvement is inconsistent")
        if self.decision not in {DECISION_PROPOSE_CHANGE, DECISION_NO_CHANGE}:
            raise RecommendationValidationError("unknown recommendation decision")
        if not isinstance(self.selected_candidate_ids, tuple) or any(
            not isinstance(value, str) or not value for value in self.selected_candidate_ids
        ):
            raise RecommendationValidationError("selected candidate IDs are invalid")
        if len(self.selected_candidate_ids) != len(set(self.selected_candidate_ids)):
            raise RecommendationValidationError("selected candidate IDs contain duplicates")
        if not isinstance(self.deployment_ordered_candidate_ids, tuple) or any(
            not isinstance(value, str) or not value
            for value in self.deployment_ordered_candidate_ids
        ):
            raise RecommendationValidationError("deployment candidate IDs are invalid")
        if len(self.deployment_ordered_candidate_ids) != len(
            set(self.deployment_ordered_candidate_ids)
        ):
            raise RecommendationValidationError("deployment candidate IDs contain duplicates")
        if set(self.deployment_ordered_candidate_ids) != set(self.selected_candidate_ids):
            raise RecommendationValidationError("selected and deployment memberships differ")
        if self.decision == DECISION_NO_CHANGE and self.selected_candidate_ids:
            raise RecommendationValidationError("no-change recommendation has selected candidates")
        if self.decision == DECISION_PROPOSE_CHANGE:
            if not self.selected_candidate_ids:
                raise RecommendationValidationError("propose-change recommendation is empty")
            if not self.final_objective < self.baseline_objective:
                raise RecommendationValidationError("proposed change does not strictly improve")
        elif self.ddl_plan or self.selected_candidates:
            raise RecommendationValidationError(
                "no-change recommendation contains deployment state"
            )
        if not isinstance(self.selected_candidates, tuple) or any(
            not isinstance(value, SelectedCandidate) for value in self.selected_candidates
        ):
            raise RecommendationValidationError("selected candidate records are invalid")
        if tuple(value.candidate_id for value in self.selected_candidates) != (
            self.deployment_ordered_candidate_ids
        ):
            raise RecommendationValidationError("selected candidate record order is inconsistent")
        names = [
            (value.statistics_object.schema, value.statistics_object.name)
            for value in self.selected_candidates
        ]
        if len(names) != len(set(names)):
            raise RecommendationValidationError("statistics object names are not unique")
        self._validate_actions()
        if not isinstance(self.runtime_metadata, Mapping):
            raise RecommendationValidationError("recommendation runtime_metadata must be an object")
        object.__setattr__(self, "runtime_metadata", MappingProxyType(dict(self.runtime_metadata)))
        if self.created_at is not None:
            _token(self.created_at, "recommendation created_at")
        if self.semantic_digest is not None:
            _digest(self.semantic_digest, "recommendation semantic digest")

    def _validate_actions(self) -> None:
        if self.decision == DECISION_NO_CHANGE:
            if self.ddl_plan:
                raise RecommendationValidationError("no-change recommendation has DDL actions")
            return
        expected_count = len(self.selected_candidates) * 2 + 1
        if len(self.ddl_plan) != expected_count:
            raise RecommendationValidationError("DDL action count is inconsistent")
        for index, candidate in enumerate(self.selected_candidates):
            create = self.ddl_plan[index * 2]
            alter = self.ddl_plan[index * 2 + 1]
            if not isinstance(create, CreateStatisticsAction) or not isinstance(
                alter, SetStatisticsTargetAction
            ):
                raise RecommendationValidationError("DDL action order is invalid")
            if (
                create.candidate_id != candidate.candidate_id
                or create.relation_id != candidate.relation_id
                or create.object_schema != candidate.statistics_object.schema
                or create.object_name != candidate.statistics_object.name
                or create.statistics_kind != candidate.kind
                or create.column_names != candidate.column_names
                or alter.candidate_id != candidate.candidate_id
                or alter.object_schema != candidate.statistics_object.schema
                or alter.object_name != candidate.statistics_object.name
                or alter.statistics_target != candidate.statistics_target
            ):
                raise RecommendationValidationError("DDL action does not match candidate record")
        analyze = self.ddl_plan[-1]
        if (
            not isinstance(analyze, AnalyzeRelationAction)
            or analyze.relation != self.target_relation
        ):
            raise RecommendationValidationError("DDL plan must end with one target ANALYZE")

    def semantic_manifest(self) -> dict[str, Any]:
        return {
            "format_version": RECOMMENDATION_FORMAT_VERSION,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "candidate_universe_semantic_digest": self.candidate_universe_semantic_digest,
            "native_stats_repository_semantic_digest": self.native_stats_repository_semantic_digest,
            "ground_truth_semantic_digest": self.ground_truth_semantic_digest,
            "singleton_profile_semantic_digest": self.singleton_profile_semantic_digest,
            "optimization_plan_semantic_digest": self.optimization_plan_semantic_digest,
            "search_result_semantic_digest": self.search_result_semantic_digest,
            "dbms": {"name": self.dbms_name, "source_version": self.dbms_source_version},
            "deployment_contract": self.deployment_contract,
            "precedence": {
                "source": self.precedence_source,
                "policy": self.precedence_policy,
                "naming_policy": self.naming_policy,
                "physical_order_contract": self.physical_order_contract,
            },
            "search": {
                "policy": self.search_policy,
                "termination_reason": self.search_termination_reason,
                "baseline_objective": self.baseline_objective,
                "final_objective": self.final_objective,
                "improvement": self.improvement,
            },
            "decision": self.decision,
            "target_relation": self.target_relation.to_dict(),
            "selected_candidate_ids": list(self.selected_candidate_ids),
            "deployment_ordered_candidate_ids": list(self.deployment_ordered_candidate_ids),
            "selected_candidates": [item.to_dict() for item in self.selected_candidates],
            "ddl_plan": [item.to_dict() for item in self.ddl_plan],
        }

    @property
    def computed_semantic_digest(self) -> str:
        from extstats_advisor.canonical import digest_json

        return digest_json(self.semantic_manifest())

    @property
    def requires_analyze(self) -> bool:
        return self.decision == DECISION_PROPOSE_CHANGE

    def to_manifest(self) -> dict[str, Any]:
        value = self.semantic_manifest()
        value["semantic_digest"] = self.computed_semantic_digest
        if self.created_at is not None:
            value["created_at"] = self.created_at
        if self.runtime_metadata:
            value["runtime_metadata"] = dict(self.runtime_metadata)
        return value


__all__ = [
    "DECISION_NO_CHANGE",
    "DECISION_PROPOSE_CHANGE",
    "POSTGRES_DEPLOYMENT_CONTRACT",
    "POSTGRES_NAMING_POLICY",
    "POSTGRES_PHYSICAL_ORDER_CONTRACT",
    "POSTGRES_STATISTICS_KINDS",
    "PRECEDENCE_POLICY",
    "PRECEDENCE_SOURCE",
    "RECOMMENDATION_FORMAT_VERSION",
    "AnalyzeRelationAction",
    "CreateStatisticsAction",
    "Recommendation",
    "SelectedCandidate",
    "SetStatisticsTargetAction",
    "StatisticsObject",
    "TargetRelation",
]
