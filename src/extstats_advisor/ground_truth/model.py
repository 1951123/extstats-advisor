"""DBMS-neutral immutable exact-cardinality truth models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from extstats_advisor.canonical import digest_json
from extstats_advisor.errors import GroundTruthValidationError

GROUND_TRUTH_FORMAT_VERSION = "ground-truth-set-v1"
GROUND_TRUTH_COLLECTION_CONTRACT = "production-exact-cardinality-v1"
PRODUCTION_EXACT_SOURCE = "production-exact-execution"
AUTHORITATIVE_EXTERNAL_SOURCE = "authoritative-external-exact"
AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT = "authoritative-external-exact-cardinality-v1"
# Descriptive aliases keep the public contract discoverable without creating a
# second wire value.
AUTHORITATIVE_EXTERNAL_EXACT_SOURCE = AUTHORITATIVE_EXTERNAL_SOURCE
AUTHORITATIVE_EXTERNAL_EXACT_COLLECTION_CONTRACT = AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT
EXTERNAL_EXACT_SOURCE = AUTHORITATIVE_EXTERNAL_SOURCE
EXTERNAL_EXACT_COLLECTION_CONTRACT = AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroundTruthValidationError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class CardinalityTruth:
    query_id: str
    cardinality: int
    source: str

    def __post_init__(self) -> None:
        _text(self.query_id, "truth query_id")
        if (
            not isinstance(self.cardinality, int)
            or isinstance(self.cardinality, bool)
            or self.cardinality < 0
        ):
            raise GroundTruthValidationError("truth cardinality must be a non-negative integer")
        _text(self.source, "truth source")

    def to_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "cardinality": self.cardinality,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class GroundTruthSource:
    kind: str
    dbms: str | None = None
    server_version: str | None = None
    server_version_num: int | None = None
    source_view_token: str | None = None
    authority: str | None = None
    dataset_identity: str | None = None
    source_revision: str | None = None
    source_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind == PRODUCTION_EXACT_SOURCE:
            _text(self.dbms, "truth source DBMS")
            _text(self.server_version, "truth source server_version")
            if (
                not isinstance(self.server_version_num, int)
                or isinstance(self.server_version_num, bool)
                or self.server_version_num < 1
            ):
                raise GroundTruthValidationError("truth source server_version_num is invalid")
            _text(self.source_view_token, "truth source_view_token")
            if any(
                value is not None
                for value in (
                    self.authority,
                    self.dataset_identity,
                    self.source_revision,
                    self.source_artifact_sha256,
                )
            ):
                raise GroundTruthValidationError(
                    "production exact source contains external provenance fields"
                )
            return
        if self.kind == AUTHORITATIVE_EXTERNAL_SOURCE:
            _text(self.authority, "truth source authority")
            _text(self.dataset_identity, "truth source dataset_identity")
            _text(self.source_revision, "truth source source_revision")
            if not isinstance(self.source_artifact_sha256, str) or not _SHA256.fullmatch(
                self.source_artifact_sha256
            ):
                raise GroundTruthValidationError("truth source artifact SHA256 is invalid")
            if any(
                value is not None
                for value in (
                    self.dbms,
                    self.server_version,
                    self.server_version_num,
                    self.source_view_token,
                )
            ):
                raise GroundTruthValidationError(
                    "external exact source contains PostgreSQL provenance fields"
                )
            return
        raise GroundTruthValidationError("unsupported ground-truth source kind")

    def to_dict(self) -> dict[str, Any]:
        if self.kind == PRODUCTION_EXACT_SOURCE:
            return {
                "kind": self.kind,
                "dbms": self.dbms,
                "server_version": self.server_version,
                "server_version_num": self.server_version_num,
                "source_view_token": self.source_view_token,
            }
        return {
            "kind": self.kind,
            "authority": self.authority,
            "dataset_identity": self.dataset_identity,
            "source_revision": self.source_revision,
            "source_artifact_sha256": self.source_artifact_sha256,
        }


@dataclass(frozen=True, slots=True)
class GroundTruthSet:
    source_snapshot_semantic_digest: str
    workload_id: str
    source: GroundTruthSource
    truths: tuple[CardinalityTruth, ...]
    collection_contract: str | None = None
    created_at: str | None = None
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    semantic_digest: str | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.source_snapshot_semantic_digest):
            raise GroundTruthValidationError("source snapshot semantic digest is invalid")
        _text(self.workload_id, "truth workload_id")
        expected_contract = (
            GROUND_TRUTH_COLLECTION_CONTRACT
            if self.source.kind == PRODUCTION_EXACT_SOURCE
            else AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT
        )
        if self.collection_contract is None:
            object.__setattr__(self, "collection_contract", expected_contract)
        elif self.collection_contract != expected_contract:
            raise GroundTruthValidationError(
                "ground-truth collection contract does not match source"
            )
        if not self.truths:
            raise GroundTruthValidationError("ground-truth set must contain at least one truth")
        ids = [truth.query_id for truth in self.truths]
        if len(ids) != len(set(ids)):
            raise GroundTruthValidationError("ground-truth query IDs must be unique")
        if any(truth.source != self.source.kind for truth in self.truths):
            raise GroundTruthValidationError("truth source does not match enclosing source")
        if not isinstance(self.runtime_metadata, Mapping):
            raise GroundTruthValidationError("ground-truth runtime_metadata must be an object")
        if self.created_at is not None:
            _text(self.created_at, "ground-truth created_at")
        if self.semantic_digest is not None and not _SHA256.fullmatch(self.semantic_digest):
            raise GroundTruthValidationError("ground-truth semantic_digest is invalid")

    def semantic_manifest(self) -> dict[str, Any]:
        return {
            "format_version": GROUND_TRUTH_FORMAT_VERSION,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "workload_id": self.workload_id,
            "source": self.source.to_dict(),
            "truths": [
                truth.to_dict() for truth in sorted(self.truths, key=lambda item: item.query_id)
            ],
            "collection_contract": self.collection_contract,
        }

    @property
    def computed_semantic_digest(self) -> str:
        return digest_json(self.semantic_manifest())

    def to_manifest(self, *, created_at: str) -> dict[str, Any]:
        value = self.semantic_manifest()
        value.update(
            {
                "created_at": created_at,
                "runtime_metadata": dict(self.runtime_metadata),
                "semantic_digest": self.computed_semantic_digest,
            }
        )
        return value
