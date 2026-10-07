"""Canonical, atomic DeploymentResult v1 artifact operations."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import canonical_json
from extstats_advisor.deployment.model import (
    DEPLOYMENT_POLICY,
    DEPLOYMENT_RESULT_FORMAT_VERSION,
    DeployedObject,
    DeploymentResult,
)
from extstats_advisor.errors import DeploymentValidationError

_TOP_LEVEL_FIELDS = {
    "format_version",
    "recommendation_semantic_digest",
    "source_snapshot_semantic_digest",
    "candidate_universe_semantic_digest",
    "native_stats_repository_semantic_digest",
    "singleton_profile_semantic_digest",
    "optimization_plan_semantic_digest",
    "search_result_semantic_digest",
    "deployment_contract",
    "deployment_policy",
    "target",
    "server",
    "decision",
    "deployment_ordered_candidate_ids",
    "deployed_objects",
    "preflight_summary",
    "commit_status",
    "post_commit_verified",
    "execution_policy",
    "semantic_digest",
    "created_at",
    "runtime_metadata",
}


def _read(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise DeploymentValidationError("deployment result must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentValidationError("invalid deployment result JSON") from exc
    if not isinstance(value, dict):
        raise DeploymentValidationError("deployment result must be an object")
    return value


def _section(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DeploymentValidationError(f"{label} is malformed")
    return value


def _from_manifest(value: dict[str, Any]) -> DeploymentResult:
    if set(value) - _TOP_LEVEL_FIELDS:
        raise DeploymentValidationError("deployment result contains unknown fields")
    if value.get("format_version") != DEPLOYMENT_RESULT_FORMAT_VERSION:
        raise DeploymentValidationError("unknown deployment result format")
    target = _section(
        value.get("target"), {"catalog", "schema", "relation", "relation_oid"}, "target"
    )
    server = _section(value.get("server"), {"server_version", "server_version_num"}, "server")
    raw_objects = value.get("deployed_objects")
    if not isinstance(raw_objects, list):
        raise DeploymentValidationError("deployed_objects must be a list")
    objects = []
    for raw in raw_objects:
        if not isinstance(raw, dict) or set(raw) != {
            "candidate_id",
            "schema",
            "name",
            "oid",
            "kind",
            "column_ordinals",
            "statistics_target",
            "payload_verified",
            "deployment_order_position",
        }:
            raise DeploymentValidationError("deployed object is malformed")
        try:
            objects.append(
                DeployedObject(
                    raw["candidate_id"],
                    raw["schema"],
                    raw["name"],
                    raw["oid"],
                    raw["kind"],
                    tuple(raw["column_ordinals"]),
                    raw["statistics_target"],
                    raw["payload_verified"],
                    raw["deployment_order_position"],
                )
            )
        except (KeyError, TypeError) as exc:
            raise DeploymentValidationError("deployed object is malformed") from exc
    try:
        result = DeploymentResult(
            value["recommendation_semantic_digest"],
            value["source_snapshot_semantic_digest"],
            value["candidate_universe_semantic_digest"],
            value["native_stats_repository_semantic_digest"],
            value["singleton_profile_semantic_digest"],
            value["optimization_plan_semantic_digest"],
            value["search_result_semantic_digest"],
            value["deployment_contract"],
            value["deployment_policy"],
            target["catalog"],
            target["schema"],
            target["relation"],
            target["relation_oid"],
            server["server_version"],
            server["server_version_num"],
            value["decision"],
            tuple(value["deployment_ordered_candidate_ids"]),
            tuple(objects),
            value["preflight_summary"],
            value["commit_status"],
            value["post_commit_verified"],
            value.get("execution_policy", {}),
            value.get("runtime_metadata", {}),
            value.get("created_at"),
            value.get("semantic_digest"),
        )
    except (KeyError, TypeError) as exc:
        raise DeploymentValidationError("deployment result is incomplete") from exc
    if result.semantic_digest != result.computed_semantic_digest:
        raise DeploymentValidationError("deployment result semantic digest mismatch")
    return result


def load_deployment_result(path: Path) -> DeploymentResult:
    return _from_manifest(_read(Path(path)))


def write_deployment_result(result: DeploymentResult, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"deployment result destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created_at = result.created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    value = DeploymentResult(
        result.recommendation_semantic_digest,
        result.source_snapshot_semantic_digest,
        result.candidate_universe_semantic_digest,
        result.native_stats_repository_semantic_digest,
        result.singleton_profile_semantic_digest,
        result.optimization_plan_semantic_digest,
        result.search_result_semantic_digest,
        result.deployment_contract,
        result.deployment_policy,
        result.target_catalog,
        result.target_schema,
        result.target_relation,
        result.target_relation_oid,
        result.server_version,
        result.server_version_num,
        result.decision,
        result.deployment_ordered_candidate_ids,
        result.deployed_objects,
        result.preflight_summary,
        result.commit_status,
        result.post_commit_verified,
        result.execution_policy,
        result.runtime_metadata,
        created_at,
    )
    temporary_fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(temporary_fd)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(value.to_manifest()) + b"\n")
        load_deployment_result(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return value.computed_semantic_digest


def validate_deployment_result(
    path: Path,
    recommendation: Any | None = None,
    *,
    source_snapshot: Any | None = None,
    candidate_universe: Any | None = None,
    native_repository: Any | None = None,
    singleton_profile: Any | None = None,
    optimization_plan: Any | None = None,
    search_result: Any | None = None,
) -> dict[str, Any]:
    result = load_deployment_result(path)
    if recommendation is not None:
        if result.recommendation_semantic_digest != recommendation.computed_semantic_digest:
            raise DeploymentValidationError("deployment recommendation digest mismatch")
        if tuple(result.deployment_ordered_candidate_ids) != tuple(
            recommendation.deployment_ordered_candidate_ids
        ):
            raise DeploymentValidationError("deployment order does not match Recommendation")
        if result.decision != recommendation.decision:
            raise DeploymentValidationError("deployment decision does not match Recommendation")
        target = recommendation.target_relation
        if (
            result.target_catalog != target.catalog
            or result.target_schema != target.schema
            or result.target_relation != target.name
        ):
            raise DeploymentValidationError("deployment target does not match Recommendation")
        if recommendation.decision == "no-change":
            if (
                result.target_relation_oid is not None
                or result.commit_status != "not-required"
                or result.deployed_objects
                or result.deployment_ordered_candidate_ids
            ):
                raise DeploymentValidationError(
                    "no-change deployment result contains production state"
                )
        else:
            expected_candidates = recommendation.selected_candidates
            if len(result.deployed_objects) != len(expected_candidates):
                raise DeploymentValidationError(
                    "deployed object count does not match Recommendation membership"
                )
            for expected_candidate, actual_object in zip(
                expected_candidates, result.deployed_objects, strict=True
            ):
                expected_fields = (
                    expected_candidate.candidate_id,
                    expected_candidate.statistics_object.schema,
                    expected_candidate.statistics_object.name,
                    expected_candidate.kind,
                    expected_candidate.column_ordinals,
                    expected_candidate.statistics_target,
                    expected_candidate.deployment_order_position,
                )
                actual_fields = (
                    actual_object.candidate_id,
                    actual_object.schema,
                    actual_object.name,
                    actual_object.kind,
                    actual_object.column_ordinals,
                    actual_object.statistics_target,
                    actual_object.deployment_order_position,
                )
                if actual_fields != expected_fields:
                    raise DeploymentValidationError(
                        f"deployed object mapping differs for {expected_candidate.candidate_id}"
                    )
                if not actual_object.payload_verified or actual_object.oid < 1:
                    raise DeploymentValidationError(
                        f"deployed object evidence is invalid for {expected_candidate.candidate_id}"
                    )
            if len({item.oid for item in result.deployed_objects}) != len(result.deployed_objects):
                raise DeploymentValidationError("deployed object OIDs are not unique")
            if tuple(
                item.candidate_id
                for item in sorted(result.deployed_objects, key=lambda item: item.oid)
            ) != tuple(recommendation.deployment_ordered_candidate_ids):
                raise DeploymentValidationError(
                    "recorded deployed OID order differs from Recommendation"
                )
            if result.target_relation_oid is None or result.target_relation_oid < 1:
                raise DeploymentValidationError("successful deployment has no relation OID")
        if native_repository is not None:
            if result.server_version != recommendation.dbms_source_version:
                raise DeploymentValidationError(
                    "deployment server version differs from Recommendation"
                )
            if result.server_version_num != native_repository.server_version_num:
                raise DeploymentValidationError(
                    "deployment server version number differs from source"
                )
        for label, actual, expected in (
            (
                "snapshot",
                result.source_snapshot_semantic_digest,
                recommendation.source_snapshot_semantic_digest,
            ),
            (
                "candidate universe",
                result.candidate_universe_semantic_digest,
                recommendation.candidate_universe_semantic_digest,
            ),
            (
                "native repository",
                result.native_stats_repository_semantic_digest,
                recommendation.native_stats_repository_semantic_digest,
            ),
            (
                "singleton profile",
                result.singleton_profile_semantic_digest,
                recommendation.singleton_profile_semantic_digest,
            ),
            (
                "optimization plan",
                result.optimization_plan_semantic_digest,
                recommendation.optimization_plan_semantic_digest,
            ),
            (
                "search result",
                result.search_result_semantic_digest,
                recommendation.search_result_semantic_digest,
            ),
        ):
            if actual != expected:
                raise DeploymentValidationError(f"deployment {label} digest mismatch")
    for label, source, expected in (
        ("snapshot", source_snapshot, result.source_snapshot_semantic_digest),
        ("candidate universe", candidate_universe, result.candidate_universe_semantic_digest),
        ("native repository", native_repository, result.native_stats_repository_semantic_digest),
        ("singleton profile", singleton_profile, result.singleton_profile_semantic_digest),
        ("optimization plan", optimization_plan, result.optimization_plan_semantic_digest),
        ("search result", search_result, result.search_result_semantic_digest),
    ):
        source_digest = getattr(source, "semantic_digest", None) if source is not None else None
        if source is not None and source_digest is None:
            source_digest = getattr(source, "computed_semantic_digest", None)
        if source is not None and source_digest != expected:
            raise DeploymentValidationError(f"deployment {label} source digest mismatch")
    return deployment_result_summary(result)


def deployment_result_summary(result: DeploymentResult) -> dict[str, Any]:
    return {
        "format_version": DEPLOYMENT_RESULT_FORMAT_VERSION,
        "semantic_digest": result.computed_semantic_digest,
        "recommendation_semantic_digest": result.recommendation_semantic_digest,
        "deployment_contract": result.deployment_contract,
        "deployment_policy": DEPLOYMENT_POLICY,
        "decision": result.decision,
        "target": {
            "catalog": result.target_catalog,
            "schema": result.target_schema,
            "relation": result.target_relation,
            "relation_oid": result.target_relation_oid,
        },
        "deployment_ordered_candidate_ids": list(result.deployment_ordered_candidate_ids),
        "selected_candidate_count": len(result.deployment_ordered_candidate_ids),
        "physical_statistics_object_count": len(result.deployed_objects),
        "deployed_object_count": len(result.deployed_objects),
        "commit_status": result.commit_status,
        "post_commit_verified": result.post_commit_verified,
    }


def inspect_deployment_result(path: Path) -> dict[str, Any]:
    return deployment_result_summary(load_deployment_result(path))
