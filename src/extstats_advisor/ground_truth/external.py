"""Import of DBMS-neutral authoritative exact-cardinality observations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import digest_bytes
from extstats_advisor.errors import GroundTruthValidationError, SnapshotValidationError
from extstats_advisor.ground_truth.model import (
    AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT,
    AUTHORITATIVE_EXTERNAL_SOURCE,
    CardinalityTruth,
    GroundTruthSet,
    GroundTruthSource,
)
from extstats_advisor.snapshot.bundle import load_snapshot, snapshot_semantic_digest
from extstats_advisor.snapshot.model import AdvisorSnapshot

AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION = "authoritative-cardinality-observations-v1"
AUTHORITATIVE_OBSERVATIONS_FORMAT_VERSION = AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object field: {key}")
        result[key] = value
    return result


def _read_observations(path: Path) -> tuple[dict[str, Any], str]:
    root = Path(path).expanduser()
    if root.is_symlink() or not root.is_file():
        raise GroundTruthValidationError("authoritative observations must be a regular file")
    try:

        def reject_constant(value: str) -> Any:
            raise ValueError(value)

        payload = root.read_bytes()
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise GroundTruthValidationError("invalid authoritative observations JSON") from exc
    if not isinstance(value, dict):
        raise GroundTruthValidationError("authoritative observations must be an object")
    return value, digest_bytes(payload)


def _sealed_snapshot(snapshot: AdvisorSnapshot | str | Path) -> AdvisorSnapshot:
    if isinstance(snapshot, (str, Path)):
        return load_snapshot(Path(snapshot))
    if not isinstance(snapshot, AdvisorSnapshot):
        raise GroundTruthValidationError("snapshot must be a sealed AdvisorSnapshot or directory")
    if snapshot.semantic_digest is None:
        raise GroundTruthValidationError("snapshot must be sealed before truth import")
    try:
        computed = snapshot_semantic_digest(snapshot)
    except SnapshotValidationError as exc:
        raise GroundTruthValidationError("snapshot is not a valid sealed AdvisorSnapshot") from exc
    if computed != snapshot.semantic_digest:
        raise GroundTruthValidationError("snapshot semantic digest does not match its contents")
    return snapshot


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GroundTruthValidationError(f"{label} must be a non-empty string")
    return value


def _observations(value: dict[str, Any]) -> tuple[str, tuple[tuple[str, int], ...]]:
    if set(value) != {"format_version", "workload_id", "truths"}:
        raise GroundTruthValidationError("authoritative observations contain unknown fields")
    if value["format_version"] != AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION:
        raise GroundTruthValidationError("unknown authoritative observations format version")
    workload_id = _text(value["workload_id"], "observations workload_id")
    raw_truths = value["truths"]
    if not isinstance(raw_truths, list) or not raw_truths:
        raise GroundTruthValidationError(
            "authoritative observations truths must be a non-empty list"
        )
    records: list[tuple[str, int]] = []
    seen: set[str] = set()
    for raw in raw_truths:
        if not isinstance(raw, dict) or set(raw) != {"query_id", "cardinality"}:
            raise GroundTruthValidationError("malformed authoritative observation")
        query_id = _text(raw["query_id"], "observation query_id")
        if query_id in seen:
            raise GroundTruthValidationError("authoritative observation query IDs must be unique")
        cardinality = raw["cardinality"]
        if not isinstance(cardinality, int) or isinstance(cardinality, bool) or cardinality < 0:
            raise GroundTruthValidationError(
                "authoritative observation cardinality must be a non-negative integer"
            )
        seen.add(query_id)
        records.append((query_id, cardinality))
    return workload_id, tuple(records)


def import_authoritative_ground_truth(
    snapshot: AdvisorSnapshot | str | Path,
    observations: str | Path,
    *,
    authority: str,
    dataset_identity: str,
    source_revision: str,
) -> GroundTruthSet:
    """Bind a validated authoritative observations file to a sealed snapshot."""

    sealed = _sealed_snapshot(snapshot)
    value, artifact_sha256 = _read_observations(Path(observations))
    workload_id, records = _observations(value)
    if workload_id != sealed.workload.workload_id:
        raise GroundTruthValidationError("observations workload ID does not match snapshot")
    workload_queries = {query.query_id: query for query in sealed.workload.queries}
    unknown = {query_id for query_id, _ in records} - workload_queries.keys()
    if unknown:
        raise GroundTruthValidationError(
            f"authoritative observations contain unknown workload queries: {sorted(unknown)}"
        )
    observed_ids = {query_id for query_id, _ in records}
    required = {query.query_id for query in sealed.workload.queries if query.weight > 0}
    missing = required - observed_ids
    if missing:
        raise GroundTruthValidationError(
            f"authoritative observations are missing positive-weight queries: {sorted(missing)}"
        )
    source = GroundTruthSource(
        AUTHORITATIVE_EXTERNAL_SOURCE,
        authority=_text(authority, "authority"),
        dataset_identity=_text(dataset_identity, "dataset_identity"),
        source_revision=_text(source_revision, "source_revision"),
        source_artifact_sha256=artifact_sha256,
    )
    return GroundTruthSet(
        sealed.semantic_digest or "",
        sealed.workload.workload_id,
        source,
        tuple(
            CardinalityTruth(query_id, cardinality, AUTHORITATIVE_EXTERNAL_SOURCE)
            for query_id, cardinality in records
        ),
        AUTHORITATIVE_EXTERNAL_COLLECTION_CONTRACT,
    )


def validate_authoritative_observations(path: str | Path) -> dict[str, Any]:
    """Validate an observations artifact and return non-sensitive structural metadata."""

    value, artifact_sha256 = _read_observations(Path(path))
    workload_id, records = _observations(value)
    return {
        "format_version": AUTHORITATIVE_CARDINALITY_OBSERVATIONS_FORMAT_VERSION,
        "workload_id": workload_id,
        "query_count": len(records),
        "source_artifact_sha256": artifact_sha256,
    }
