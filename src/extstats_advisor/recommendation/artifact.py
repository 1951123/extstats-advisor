"""Canonical persistence and source-aware validation for Recommendation v1."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import canonical_json
from extstats_advisor.dbms.postgres.recommendation import build_postgres_recommendation
from extstats_advisor.errors import RecommendationValidationError
from extstats_advisor.recommendation.model import (
    RECOMMENDATION_FORMAT_VERSION,
    AnalyzeRelationAction,
    CreateStatisticsAction,
    Recommendation,
    SelectedCandidate,
    SetStatisticsTargetAction,
    StatisticsObject,
    TargetRelation,
)

_TOP_LEVEL_FIELDS = {
    "format_version",
    "source_snapshot_semantic_digest",
    "candidate_universe_semantic_digest",
    "native_stats_repository_semantic_digest",
    "ground_truth_semantic_digest",
    "singleton_profile_semantic_digest",
    "optimization_plan_semantic_digest",
    "search_result_semantic_digest",
    "dbms",
    "deployment_contract",
    "precedence",
    "search",
    "decision",
    "target_relation",
    "selected_candidate_ids",
    "deployment_ordered_candidate_ids",
    "selected_candidates",
    "ddl_plan",
    "semantic_digest",
    "created_at",
    "runtime_metadata",
}


def _read(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise RecommendationValidationError("recommendation must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecommendationValidationError("invalid recommendation JSON") from exc
    if not isinstance(value, dict):
        raise RecommendationValidationError("recommendation must be an object")
    return value


def _strict_dict(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RecommendationValidationError(f"{label} section is invalid")
    return value


def _from_manifest(value: dict[str, Any]) -> Recommendation:
    unknown = set(value) - _TOP_LEVEL_FIELDS
    if unknown:
        raise RecommendationValidationError("recommendation contains unknown fields")
    if value.get("format_version") != RECOMMENDATION_FORMAT_VERSION:
        raise RecommendationValidationError("unknown recommendation format")
    dbms = _strict_dict(value.get("dbms"), {"name", "source_version"}, "recommendation DBMS")
    precedence = _strict_dict(
        value.get("precedence"),
        {"source", "policy", "naming_policy", "physical_order_contract"},
        "recommendation precedence",
    )
    search = _strict_dict(
        value.get("search"),
        {"policy", "termination_reason", "baseline_objective", "final_objective", "improvement"},
        "recommendation search",
    )
    target_value = _strict_dict(
        value.get("target_relation"),
        {"relation_id", "catalog", "schema", "name"},
        "recommendation target relation",
    )
    selected_values = value.get("selected_candidates")
    actions_value = value.get("ddl_plan")
    if not isinstance(selected_values, list) or not isinstance(actions_value, list):
        raise RecommendationValidationError("recommendation candidate/action lists are invalid")
    try:
        selected = []
        for item in selected_values:
            if not isinstance(item, dict) or set(item) != {
                "candidate_id",
                "kind",
                "relation_id",
                "column_ordinals",
                "column_names",
                "global_frozen_precedence_rank",
                "deployment_order_position",
                "statistics_object",
                "statistics_target",
            }:
                raise RecommendationValidationError(
                    "recommendation selected candidate is malformed"
                )
            object_value = _strict_dict(
                item["statistics_object"], {"schema", "name"}, "recommendation statistics object"
            )
            selected.append(
                SelectedCandidate(
                    item["candidate_id"],
                    item["kind"],
                    item["relation_id"],
                    tuple(item["column_ordinals"]),
                    tuple(item["column_names"]),
                    item["global_frozen_precedence_rank"],
                    item["deployment_order_position"],
                    StatisticsObject(object_value["schema"], object_value["name"]),
                    item["statistics_target"],
                )
            )
        actions = []
        for item in actions_value:
            if not isinstance(item, dict) or "action" not in item:
                raise RecommendationValidationError("recommendation DDL action is malformed")
            action = item["action"]
            if action == "create-statistics":
                if set(item) != {
                    "action",
                    "candidate_id",
                    "relation_id",
                    "object_schema",
                    "object_name",
                    "statistics_kind",
                    "column_names",
                }:
                    raise RecommendationValidationError("recommendation CREATE action is malformed")
                actions.append(
                    CreateStatisticsAction(
                        item["candidate_id"],
                        item["relation_id"],
                        item["object_schema"],
                        item["object_name"],
                        item["statistics_kind"],
                        tuple(item["column_names"]),
                    )
                )
            elif action == "set-statistics-target":
                if set(item) != {
                    "action",
                    "candidate_id",
                    "object_schema",
                    "object_name",
                    "statistics_target",
                }:
                    raise RecommendationValidationError("recommendation ALTER action is malformed")
                actions.append(
                    SetStatisticsTargetAction(
                        item["candidate_id"],
                        item["object_schema"],
                        item["object_name"],
                        item["statistics_target"],
                    )
                )
            elif action == "analyze-relation":
                if set(item) != {"action", "relation"}:
                    raise RecommendationValidationError(
                        "recommendation ANALYZE action is malformed"
                    )
                relation_value = _strict_dict(
                    item["relation"],
                    {"relation_id", "catalog", "schema", "name"},
                    "recommendation ANALYZE relation",
                )
                actions.append(
                    AnalyzeRelationAction(
                        TargetRelation(
                            relation_value["relation_id"],
                            relation_value["catalog"],
                            relation_value["schema"],
                            relation_value["name"],
                        )
                    )
                )
            else:
                raise RecommendationValidationError("unknown recommendation DDL action")
        recommendation = Recommendation(
            value["source_snapshot_semantic_digest"],
            value["candidate_universe_semantic_digest"],
            value["native_stats_repository_semantic_digest"],
            value["ground_truth_semantic_digest"],
            value["singleton_profile_semantic_digest"],
            value["optimization_plan_semantic_digest"],
            value["search_result_semantic_digest"],
            dbms["name"],
            dbms["source_version"],
            value["deployment_contract"],
            precedence["source"],
            precedence["policy"],
            precedence["naming_policy"],
            precedence["physical_order_contract"],
            search["policy"],
            search["termination_reason"],
            search["baseline_objective"],
            search["final_objective"],
            search["improvement"],
            value["decision"],
            TargetRelation(
                target_value["relation_id"],
                target_value["catalog"],
                target_value["schema"],
                target_value["name"],
            ),
            tuple(value["selected_candidate_ids"]),
            tuple(value["deployment_ordered_candidate_ids"]),
            tuple(selected),
            tuple(actions),
            value.get("runtime_metadata", {}),
            value.get("created_at"),
            value.get("semantic_digest"),
        )
    except RecommendationValidationError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RecommendationValidationError("recommendation is incomplete or invalid") from exc
    if recommendation.semantic_digest != recommendation.computed_semantic_digest:
        raise RecommendationValidationError("recommendation semantic digest mismatch")
    return recommendation


def load_recommendation(path: Path) -> Recommendation:
    return _from_manifest(_read(Path(path)))


def write_recommendation(recommendation: Recommendation, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"recommendation destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(recommendation.to_manifest()) + b"\n")
        load_recommendation(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return recommendation.computed_semantic_digest


def _ground_truth_digest(ground_truth: Any) -> str:
    digest = getattr(ground_truth, "semantic_digest", None)
    return digest if isinstance(digest, str) else ground_truth.computed_semantic_digest


def validate_recommendation(
    path: Path,
    snapshot: Any | None = None,
    candidate_universe: Any | None = None,
    native_repository: Any | None = None,
    ground_truth: Any | None = None,
    singleton_profile: Any | None = None,
    optimization_plan: Any | None = None,
    search_result: Any | None = None,
) -> dict[str, Any]:
    recommendation = load_recommendation(path)
    for label, source, expected in (
        ("snapshot", snapshot, recommendation.source_snapshot_semantic_digest),
        (
            "candidate universe",
            candidate_universe,
            recommendation.candidate_universe_semantic_digest,
        ),
        (
            "native repository",
            native_repository,
            recommendation.native_stats_repository_semantic_digest,
        ),
    ):
        if source is not None and getattr(source, "semantic_digest", None) != expected:
            raise RecommendationValidationError(f"recommendation {label} digest mismatch")
    if (
        ground_truth is not None
        and _ground_truth_digest(ground_truth) != recommendation.ground_truth_semantic_digest
    ):
        raise RecommendationValidationError("recommendation ground-truth digest mismatch")
    if all(
        source is not None
        for source in (
            snapshot,
            candidate_universe,
            native_repository,
            singleton_profile,
            optimization_plan,
            search_result,
        )
    ):
        expected = build_postgres_recommendation(
            snapshot,
            candidate_universe,
            native_repository,
            singleton_profile,
            optimization_plan,
            search_result,
        )
        if recommendation.computed_semantic_digest != expected.computed_semantic_digest:
            raise RecommendationValidationError(
                "recommendation does not match recomputed source-derived state"
            )
    return recommendation_summary(recommendation)


def recommendation_summary(recommendation: Recommendation) -> dict[str, Any]:
    return {
        "format_version": RECOMMENDATION_FORMAT_VERSION,
        "semantic_digest": recommendation.computed_semantic_digest,
        "decision": recommendation.decision,
        "precedence_source": recommendation.precedence_source,
        "precedence_policy": recommendation.precedence_policy,
        "search_policy": recommendation.search_policy,
        "search_termination_reason": recommendation.search_termination_reason,
        "baseline_objective": recommendation.baseline_objective,
        "final_objective": recommendation.final_objective,
        "improvement": recommendation.improvement,
        "selected_candidate_count": len(recommendation.selected_candidate_ids),
        "physical_statistics_object_count": sum(
            isinstance(action, CreateStatisticsAction) for action in recommendation.ddl_plan
        ),
        "selected_candidate_ids": list(recommendation.selected_candidate_ids),
        "deployment_ordered_candidate_ids": list(recommendation.deployment_ordered_candidate_ids),
        "target_catalog": recommendation.target_relation.catalog,
        "target_schema": recommendation.target_relation.schema,
        "target_relation": recommendation.target_relation.name,
        "statistics_target": (
            recommendation.selected_candidates[0].statistics_target
            if recommendation.selected_candidates
            else None
        ),
        "deployment_contract": recommendation.deployment_contract,
        "physical_order_contract": recommendation.physical_order_contract,
        "ddl_action_count": len(recommendation.ddl_plan),
        "requires_analyze": recommendation.requires_analyze,
        "statistics_object_names": [
            item.statistics_object.name for item in recommendation.selected_candidates
        ],
    }


def inspect_recommendation(path: Path) -> dict[str, Any]:
    return recommendation_summary(load_recommendation(path))


__all__ = [
    "inspect_recommendation",
    "load_recommendation",
    "recommendation_summary",
    "validate_recommendation",
    "write_recommendation",
]
