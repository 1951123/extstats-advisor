"""Atomic deterministic GroundTruthSet v1 JSON artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import canonical_json, digest_json
from extstats_advisor.errors import GroundTruthValidationError
from extstats_advisor.ground_truth.model import (
    GROUND_TRUTH_FORMAT_VERSION,
    CardinalityTruth,
    GroundTruthSet,
    GroundTruthSource,
)
from extstats_advisor.snapshot.model import AdvisorSnapshot


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise GroundTruthValidationError("ground-truth artifact must be a regular file")

    def reject_constant(value: str) -> None:
        raise ValueError(value)

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise GroundTruthValidationError("invalid ground-truth JSON") from exc
    if not isinstance(value, dict):
        raise GroundTruthValidationError("ground-truth artifact must be an object")
    return value


def _semantic_manifest(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "format_version": value["format_version"],
        "source_snapshot_semantic_digest": value["source_snapshot_semantic_digest"],
        "workload_id": value["workload_id"],
        "source": value["source"],
        "truths": sorted(value["truths"], key=lambda item: item["query_id"]),
        "collection_contract": value["collection_contract"],
    }


def _validate_records(value: dict[str, Any]) -> tuple[CardinalityTruth, ...]:
    raw = value.get("truths")
    if not isinstance(raw, list) or not raw:
        raise GroundTruthValidationError("ground-truth truths must be a non-empty list")
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"query_id", "cardinality", "source"}:
            raise GroundTruthValidationError("malformed ground-truth record")
        try:
            result.append(CardinalityTruth(**item))
        except TypeError as exc:
            raise GroundTruthValidationError("malformed ground-truth record") from exc
    return tuple(result)


def _ground_truth_from_manifest(value: dict[str, Any]) -> GroundTruthSet:
    allowed = {
        "format_version",
        "source_snapshot_semantic_digest",
        "workload_id",
        "source",
        "truths",
        "collection_contract",
        "created_at",
        "runtime_metadata",
        "semantic_digest",
    }
    if set(value) - allowed:
        raise GroundTruthValidationError("ground-truth artifact contains unknown fields")
    if value.get("format_version") != GROUND_TRUTH_FORMAT_VERSION:
        raise GroundTruthValidationError("unknown ground-truth format version")
    if not isinstance(value.get("source"), dict):
        raise GroundTruthValidationError("ground-truth source must be an object")
    if set(value["source"]) != {
        "kind",
        "dbms",
        "server_version",
        "server_version_num",
        "source_view_token",
    }:
        raise GroundTruthValidationError("ground-truth source fields are invalid")
    if not isinstance(value.get("runtime_metadata"), dict):
        raise GroundTruthValidationError("ground-truth runtime_metadata must be an object")
    if not isinstance(value.get("created_at"), str) or not value["created_at"].strip():
        raise GroundTruthValidationError("ground-truth created_at is required")
    truths = _validate_records(value)
    try:
        result = GroundTruthSet(
            value["source_snapshot_semantic_digest"],
            value["workload_id"],
            GroundTruthSource(**value["source"]),
            truths,
            value["collection_contract"],
            value["created_at"],
            value["runtime_metadata"],
            value.get("semantic_digest"),
        )
    except (KeyError, TypeError) as exc:
        raise GroundTruthValidationError("ground-truth manifest is incomplete") from exc
    if value.get("semantic_digest") != digest_json(_semantic_manifest(value)):
        raise GroundTruthValidationError("ground-truth semantic digest mismatch")
    if result.computed_semantic_digest != value["semantic_digest"]:
        raise GroundTruthValidationError("ground-truth semantic manifest mismatch")
    return result


def _check_snapshot_compatibility(ground_truth: GroundTruthSet, snapshot: AdvisorSnapshot) -> None:
    if snapshot.semantic_digest is None:
        raise GroundTruthValidationError("snapshot must be sealed before truth validation")
    if ground_truth.source_snapshot_semantic_digest != snapshot.semantic_digest:
        raise GroundTruthValidationError("ground-truth snapshot digest mismatch")
    if ground_truth.workload_id != snapshot.workload.workload_id:
        raise GroundTruthValidationError("ground-truth workload ID mismatch")
    if ground_truth.source.dbms != snapshot.dbms.name:
        raise GroundTruthValidationError("ground-truth DBMS mismatch")
    token = snapshot.semantic_provenance.get("source_view_token")
    if not isinstance(token, str) or token != ground_truth.source.source_view_token:
        raise GroundTruthValidationError("ground-truth source-view provenance mismatch")
    query_ids = {query.query_id for query in snapshot.workload.queries}
    truth_ids = {truth.query_id for truth in ground_truth.truths}
    if not truth_ids.issubset(query_ids):
        raise GroundTruthValidationError("ground-truth contains an unknown workload query")
    required = {query.query_id for query in snapshot.workload.queries if query.weight > 0}
    missing = required - truth_ids
    if missing:
        raise GroundTruthValidationError(
            f"ground-truth is missing positive-weight queries: {sorted(missing)}"
        )


def validate_ground_truth_set(
    path: Path, snapshot: AdvisorSnapshot | None = None
) -> dict[str, Any]:
    root = Path(path).expanduser()
    if root.is_symlink():
        raise GroundTruthValidationError("ground-truth artifact may not be a symlink")
    result = _ground_truth_from_manifest(_read_json(root.resolve()))
    if snapshot is not None:
        _check_snapshot_compatibility(result, snapshot)
    return {
        "format_version": GROUND_TRUTH_FORMAT_VERSION,
        "semantic_digest": result.computed_semantic_digest,
        "source_snapshot_semantic_digest": result.source_snapshot_semantic_digest,
        "workload_id": result.workload_id,
        "query_count": len(result.truths),
        "source_view_token": result.source.source_view_token,
    }


def load_ground_truth_set(path: Path, snapshot: AdvisorSnapshot | None = None) -> GroundTruthSet:
    root = Path(path).expanduser()
    if root.is_symlink():
        raise GroundTruthValidationError("ground-truth artifact may not be a symlink")
    root = root.resolve()
    validate_ground_truth_set(root, snapshot)
    return _ground_truth_from_manifest(_read_json(root))


def write_ground_truth_set(ground_truth: GroundTruthSet, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"ground-truth destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created_at = ground_truth.created_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    manifest = ground_truth.to_manifest(created_at=created_at)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(manifest) + b"\n")
        _ground_truth_from_manifest(_read_json(temporary))
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return manifest["semantic_digest"]
