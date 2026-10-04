"""Generic, DBMS-neutral deployment result models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from extstats_advisor.canonical import digest_json
from extstats_advisor.errors import DeploymentValidationError

DEPLOYMENT_RESULT_FORMAT_VERSION = "postgresql-deployment-result-v1"
DEPLOYMENT_POLICY = "postgresql-add-only-deployment-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise DeploymentValidationError(f"{label} must be a SHA-256 digest")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise DeploymentValidationError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class DeployedObject:
    candidate_id: str
    schema: str
    name: str
    oid: int
    kind: str
    column_ordinals: tuple[int, ...]
    statistics_target: int
    payload_verified: bool
    deployment_order_position: int

    def __post_init__(self) -> None:
        _text(self.candidate_id, "deployed candidate_id")
        _text(self.schema, "deployed schema")
        _text(self.name, "deployed name")
        if isinstance(self.oid, bool) or not isinstance(self.oid, int) or self.oid < 1:
            raise DeploymentValidationError("deployed object OID must be positive")
        if self.kind not in {"postgresql.mcv", "postgresql.dependencies"}:
            raise DeploymentValidationError("deployed object kind is unsupported")
        if (
            len(self.column_ordinals) != 2
            or tuple(sorted(self.column_ordinals)) != self.column_ordinals
        ):
            raise DeploymentValidationError("deployed column ordinals are invalid")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 1
            for value in self.column_ordinals
        ):
            raise DeploymentValidationError("deployed column ordinals are invalid")
        if (
            isinstance(self.statistics_target, bool)
            or not isinstance(self.statistics_target, int)
            or self.statistics_target < 1
        ):
            raise DeploymentValidationError("deployed statistics target is invalid")
        if not isinstance(self.payload_verified, bool):
            raise DeploymentValidationError("deployed payload_verified must be boolean")
        if (
            isinstance(self.deployment_order_position, bool)
            or not isinstance(self.deployment_order_position, int)
            or self.deployment_order_position < 1
        ):
            raise DeploymentValidationError("deployed order position is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "schema": self.schema,
            "name": self.name,
            "oid": self.oid,
            "kind": self.kind,
            "column_ordinals": list(self.column_ordinals),
            "statistics_target": self.statistics_target,
            "payload_verified": self.payload_verified,
            "deployment_order_position": self.deployment_order_position,
        }


@dataclass(frozen=True, slots=True)
class DeploymentResult:
    recommendation_semantic_digest: str
    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    native_stats_repository_semantic_digest: str
    singleton_profile_semantic_digest: str
    optimization_plan_semantic_digest: str
    search_result_semantic_digest: str
    deployment_contract: str
    deployment_policy: str
    target_catalog: str | None
    target_schema: str
    target_relation: str
    target_relation_oid: int | None
    server_version: str
    server_version_num: int
    decision: str
    deployment_ordered_candidate_ids: tuple[str, ...]
    deployed_objects: tuple[DeployedObject, ...]
    preflight_summary: Mapping[str, Any]
    commit_status: str
    post_commit_verified: bool
    execution_policy: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    created_at: str | None = field(default=None, repr=False, compare=False)
    semantic_digest: str | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        for label, value in (
            ("recommendation", self.recommendation_semantic_digest),
            ("snapshot", self.source_snapshot_semantic_digest),
            ("candidate universe", self.candidate_universe_semantic_digest),
            ("native repository", self.native_stats_repository_semantic_digest),
            ("singleton profile", self.singleton_profile_semantic_digest),
            ("optimization plan", self.optimization_plan_semantic_digest),
            ("search result", self.search_result_semantic_digest),
        ):
            _digest(value, f"deployment {label} digest")
        if self.deployment_contract != "postgresql-extended-statistics-deployment-v1":
            raise DeploymentValidationError("unsupported deployment contract")
        if self.deployment_policy != DEPLOYMENT_POLICY:
            raise DeploymentValidationError("unsupported deployment policy")
        if self.target_catalog is not None:
            _text(self.target_catalog, "target catalog")
        _text(self.target_schema, "target schema")
        _text(self.target_relation, "target relation")
        if self.target_relation_oid is not None and (
            isinstance(self.target_relation_oid, bool)
            or not isinstance(self.target_relation_oid, int)
            or self.target_relation_oid < 1
        ):
            raise DeploymentValidationError("target relation OID is invalid")
        _text(self.server_version, "server version")
        if (
            isinstance(self.server_version_num, bool)
            or not isinstance(self.server_version_num, int)
            or self.server_version_num < 1
        ):
            raise DeploymentValidationError("server version number is invalid")
        if self.decision not in {"propose-change", "no-change"}:
            raise DeploymentValidationError("deployment decision is invalid")
        if not isinstance(self.deployment_ordered_candidate_ids, tuple) or len(
            self.deployment_ordered_candidate_ids
        ) != len(set(self.deployment_ordered_candidate_ids)):
            raise DeploymentValidationError("deployment order IDs are invalid")
        if any(
            not isinstance(value, str) or not value
            for value in self.deployment_ordered_candidate_ids
        ):
            raise DeploymentValidationError("deployment order IDs are invalid")
        if not isinstance(self.deployed_objects, tuple) or any(
            not isinstance(value, DeployedObject) for value in self.deployed_objects
        ):
            raise DeploymentValidationError("deployed objects are invalid")
        if tuple(value.deployment_order_position for value in self.deployed_objects) != tuple(
            range(1, len(self.deployed_objects) + 1)
        ):
            raise DeploymentValidationError("deployed object positions are invalid")
        if (
            tuple(value.candidate_id for value in self.deployed_objects)
            != self.deployment_ordered_candidate_ids
        ):
            raise DeploymentValidationError("deployed object order does not match Recommendation")
        if self.decision == "no-change":
            if self.deployed_objects or self.deployment_ordered_candidate_ids:
                raise DeploymentValidationError("no-change result contains deployed objects")
            if self.commit_status != "not-required":
                raise DeploymentValidationError("no-change result must not claim a commit")
        else:
            if self.target_relation_oid is None:
                raise DeploymentValidationError("successful deployment has no target relation OID")
            if not self.deployed_objects:
                raise DeploymentValidationError("successful deployment has no deployed objects")
            if any(not value.payload_verified for value in self.deployed_objects):
                raise DeploymentValidationError("successful deployment has unverified payloads")
            if self.commit_status != "committed":
                raise DeploymentValidationError("non-empty deployment must be committed")
        if not self.post_commit_verified:
            raise DeploymentValidationError("published result must be post-commit verified")
        if not isinstance(self.preflight_summary, Mapping):
            raise DeploymentValidationError("preflight summary must be an object")
        if not isinstance(self.execution_policy, Mapping) or not isinstance(
            self.runtime_metadata, Mapping
        ):
            raise DeploymentValidationError("deployment metadata must be objects")
        object.__setattr__(
            self, "preflight_summary", MappingProxyType(dict(self.preflight_summary))
        )
        object.__setattr__(self, "execution_policy", MappingProxyType(dict(self.execution_policy)))
        object.__setattr__(self, "runtime_metadata", MappingProxyType(dict(self.runtime_metadata)))
        if self.created_at is not None:
            _text(self.created_at, "deployment created_at")
        if self.semantic_digest is not None:
            _digest(self.semantic_digest, "deployment semantic digest")
            if self.semantic_digest != self.computed_semantic_digest:
                raise DeploymentValidationError("deployment semantic digest is inconsistent")

    def semantic_manifest(self) -> dict[str, Any]:
        return {
            "format_version": DEPLOYMENT_RESULT_FORMAT_VERSION,
            "recommendation_semantic_digest": self.recommendation_semantic_digest,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "candidate_universe_semantic_digest": self.candidate_universe_semantic_digest,
            "native_stats_repository_semantic_digest": self.native_stats_repository_semantic_digest,
            "singleton_profile_semantic_digest": self.singleton_profile_semantic_digest,
            "optimization_plan_semantic_digest": self.optimization_plan_semantic_digest,
            "search_result_semantic_digest": self.search_result_semantic_digest,
            "deployment_contract": self.deployment_contract,
            "deployment_policy": self.deployment_policy,
            "target": {
                "catalog": self.target_catalog,
                "schema": self.target_schema,
                "relation": self.target_relation,
                "relation_oid": self.target_relation_oid,
            },
            "server": {
                "server_version": self.server_version,
                "server_version_num": self.server_version_num,
            },
            "decision": self.decision,
            "deployment_ordered_candidate_ids": list(self.deployment_ordered_candidate_ids),
            "deployed_objects": [value.to_dict() for value in self.deployed_objects],
            "preflight_summary": dict(self.preflight_summary),
            "commit_status": self.commit_status,
            "post_commit_verified": self.post_commit_verified,
            "execution_policy": dict(self.execution_policy),
        }

    @property
    def computed_semantic_digest(self) -> str:
        return digest_json(self.semantic_manifest())

    def to_manifest(self) -> dict[str, Any]:
        value = self.semantic_manifest()
        value["semantic_digest"] = self.computed_semantic_digest
        if self.created_at is not None:
            value["created_at"] = self.created_at
        if self.runtime_metadata:
            value["runtime_metadata"] = dict(self.runtime_metadata)
        return value
