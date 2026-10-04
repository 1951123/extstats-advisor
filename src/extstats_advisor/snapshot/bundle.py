"""Atomic sealed-directory AdvisorSnapshot v1 artifact operations."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import pyarrow as pa

from extstats_advisor.canonical import canonical_json, digest_bytes, digest_json
from extstats_advisor.errors import SnapshotCompatibilityError, SnapshotValidationError
from extstats_advisor.snapshot.arrow import deserialize_table, serialize_table
from extstats_advisor.snapshot.model import (
    AdvisorSnapshot,
    ColumnSchema,
    DBMSIdentity,
    PopulationMetadata,
    RelationName,
    RelationSchema,
    SampleDescriptor,
    SnapshotConsistency,
    Workload,
)

FORMAT_VERSION = "advisor-snapshot-v1"


def _json_read(path: Path) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(value)

    try:
        value = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise SnapshotValidationError(f"invalid JSON component: {path.name}") from exc
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"JSON component must be an object: {path.name}")
    return value


def _safe_regular_file(root: Path, relative: str) -> Path:
    raw = PurePosixPath(relative)
    if raw.is_absolute() or ".." in raw.parts:
        raise SnapshotValidationError(f"unsafe artifact path: {relative}")
    original = root / Path(*raw.parts)
    if original.is_symlink():
        raise SnapshotValidationError(f"symlinked artifact component: {relative}")
    candidate = original.resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise SnapshotValidationError(f"missing artifact component: {relative}")
    return candidate


def _load_schema(value: dict[str, Any]) -> tuple[RelationSchema, ...]:
    relations = value.get("relations")
    if not isinstance(relations, list):
        raise SnapshotValidationError("schema.json requires relations")
    result = []
    for raw in relations:
        if not isinstance(raw, dict) or not isinstance(raw.get("columns"), list):
            raise SnapshotValidationError("schema relation requires columns")
        try:
            columns = tuple(ColumnSchema(**column) for column in raw["columns"])
            result.append(
                RelationSchema(
                    str(raw.get("relation_id")),
                    RelationName(**raw["relation_name"]),
                    columns,
                )
            )
        except (TypeError, KeyError) as exc:
            raise SnapshotValidationError("invalid schema relation") from exc
    return tuple(result)


def _load_population(value: dict[str, Any]) -> tuple[PopulationMetadata, ...]:
    relations = value.get("relations")
    if not isinstance(relations, list):
        raise SnapshotValidationError("population.json requires relations")
    try:
        return tuple(PopulationMetadata(**raw) for raw in relations)
    except (TypeError, KeyError) as exc:
        raise SnapshotValidationError("invalid population metadata") from exc


def _load_workload(value: dict[str, Any]) -> Workload:
    return Workload.from_dict(value)


def _semantic_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit manifest subset whose meaning defines identity."""

    return {
        "format_version": manifest["format_version"],
        "dbms": manifest["dbms"],
        "component_digests": manifest["component_digests"],
        "sample_inventory": manifest["sample_inventory"],
        "snapshot_consistency": manifest["snapshot_consistency"],
        "semantic_provenance": manifest["semantic_provenance"],
        "sensitivity": manifest["sensitivity"],
    }


def _arrow_matches_schema(table: pa.Table, schema: RelationSchema) -> None:
    if len(table.schema) != len(schema.columns):
        raise SnapshotValidationError(f"Arrow/schema column count mismatch: {schema.relation_id}")
    for field, column in zip(table.schema, schema.columns, strict=True):
        if (
            field.name != column.name
            or str(field.type) != column.arrow_type
            or field.nullable != column.nullable
        ):
            raise SnapshotValidationError(
                f"Arrow/schema column mismatch: {schema.relation_id}.{column.name}"
            )


def write_snapshot(snapshot: AdvisorSnapshot, destination: Path) -> str:
    """Write, seal, verify, and atomically publish a new snapshot directory."""

    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"snapshot destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        (temporary / "samples").mkdir()
        descriptors: list[SampleDescriptor] = []
        for relation_id, table in sorted(snapshot.samples.items()):
            schema = snapshot.schema_by_id[relation_id]
            if not isinstance(table, pa.Table):
                raise SnapshotValidationError(f"sample is not an Arrow table: {relation_id}")
            _arrow_matches_schema(table, schema)
            payload = serialize_table(table)
            relative = f"samples/{digest_bytes(relation_id.encode('utf-8'))[:24]}.arrow"
            (temporary / relative).write_bytes(payload)
            descriptors.append(
                SampleDescriptor(
                    relation_id,
                    relative,
                    table.num_rows,
                    "arrow-ipc-file",
                    digest_bytes(payload),
                    {
                        "library": "pyarrow",
                        "version": pa.__version__,
                        "format": "Arrow IPC file",
                    },
                )
            )
        schema_json = {"relations": [item.to_dict() for item in snapshot.schemas]}
        population_json = {"relations": [item.to_dict() for item in snapshot.populations]}
        workload_json = snapshot.workload.to_dict()
        for name, value in (
            ("schema.json", schema_json),
            ("population.json", population_json),
            ("workload.json", workload_json),
        ):
            (temporary / name).write_bytes(canonical_json(value) + b"\n")
        component_digests = {
            "schema.json": digest_json(schema_json),
            "population.json": digest_json(population_json),
            "workload.json": digest_json(workload_json),
        }
        manifest = {
            "format_version": FORMAT_VERSION,
            "sealed": True,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "dbms": snapshot.dbms.to_dict(),
            "component_digests": component_digests,
            "sample_inventory": [
                item.to_dict() for item in sorted(descriptors, key=lambda item: item.relation_id)
            ],
            "snapshot_consistency": snapshot.consistency.to_dict(),
            "semantic_provenance": dict(snapshot.semantic_provenance),
            "runtime_metadata": dict(snapshot.runtime_metadata),
            "sensitivity": dict(snapshot.sensitivity),
        }
        manifest["semantic_digest"] = digest_json(_semantic_manifest(manifest))
        (temporary / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
        validate_snapshot(temporary)
        os.replace(temporary, destination)
        return manifest["semantic_digest"]
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _load_descriptors(manifest: dict[str, Any]) -> tuple[SampleDescriptor, ...]:
    raw = manifest.get("sample_inventory")
    if not isinstance(raw, list):
        raise SnapshotValidationError("manifest requires sample_inventory")
    try:
        return tuple(SampleDescriptor(**item) for item in raw)
    except (TypeError, KeyError) as exc:
        raise SnapshotValidationError("invalid sample descriptor") from exc


def validate_snapshot(path: Path) -> dict[str, Any]:
    original_root = Path(path).expanduser()
    if original_root.is_symlink():
        raise SnapshotValidationError("snapshot root may not be a symlink")
    root = original_root.resolve()
    if not root.is_dir():
        raise SnapshotValidationError(f"snapshot is not a directory: {path}")
    manifest = _json_read(_safe_regular_file(root, "manifest.json"))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise SnapshotCompatibilityError("unknown snapshot format version")
    if manifest.get("sealed") is not True:
        raise SnapshotValidationError("snapshot is not sealed")
    if (
        not isinstance(manifest.get("dbms"), dict)
        or "sensitivity" not in manifest
        or "semantic_provenance" not in manifest
        or "runtime_metadata" not in manifest
        or "snapshot_consistency" not in manifest
    ):
        raise SnapshotValidationError("manifest is missing required declarations")
    try:
        DBMSIdentity(**manifest["dbms"])
    except (TypeError, KeyError) as exc:
        raise SnapshotValidationError("invalid DBMS identity") from exc
    for field_name in ("semantic_provenance", "runtime_metadata", "sensitivity"):
        if not isinstance(manifest[field_name], dict):
            raise SnapshotValidationError(f"manifest field must be an object: {field_name}")
    try:
        SnapshotConsistency(**manifest["snapshot_consistency"])
    except (TypeError, KeyError) as exc:
        raise SnapshotValidationError("invalid snapshot consistency declaration") from exc
    components = {
        name: _json_read(_safe_regular_file(root, name))
        for name in ("schema.json", "population.json", "workload.json")
    }
    component_digests = manifest.get("component_digests")
    if not isinstance(component_digests, dict):
        raise SnapshotValidationError("manifest requires component_digests")
    for name, value in components.items():
        if component_digests.get(name) != digest_json(value):
            raise SnapshotValidationError(f"component digest mismatch: {name}")
    schemas = _load_schema(components["schema.json"])
    populations = _load_population(components["population.json"])
    workload = _load_workload(components["workload.json"])
    relation_ids = {item.relation_id for item in schemas}
    population_ids = {item.relation_id for item in populations}
    if len(schemas) != len(relation_ids):
        raise SnapshotValidationError("schema relation IDs must be unique")
    if len(populations) != len(population_ids) or population_ids != relation_ids:
        raise SnapshotValidationError("population metadata must cover exactly schema relations")
    descriptors = _load_descriptors(manifest)
    if (
        len(descriptors) != len(relation_ids)
        or {item.relation_id for item in descriptors} != relation_ids
    ):
        raise SnapshotValidationError("sample inventory must cover exactly schema relations")
    by_relation = {item.relation_id: item for item in schemas}
    for descriptor in descriptors:
        relative = PurePosixPath(descriptor.relative_path)
        if relative.parts[:1] != ("samples",) or relative.is_absolute() or ".." in relative.parts:
            raise SnapshotValidationError(f"unsafe sample path: {descriptor.relative_path}")
        payload = _safe_regular_file(root, descriptor.relative_path).read_bytes()
        if digest_bytes(payload) != descriptor.payload_sha256:
            raise SnapshotValidationError(
                f"sample payload digest mismatch: {descriptor.relation_id}"
            )
        table = deserialize_table(payload)
        _arrow_matches_schema(table, by_relation[descriptor.relation_id])
        if table.num_rows != descriptor.sample_row_count:
            raise SnapshotValidationError(f"sample row count mismatch: {descriptor.relation_id}")
    if manifest.get("semantic_digest") != digest_json(_semantic_manifest(manifest)):
        raise SnapshotValidationError("root semantic digest mismatch")
    return {
        "format_version": FORMAT_VERSION,
        "semantic_digest": manifest["semantic_digest"],
        "dbms": manifest["dbms"],
        "relations": [item.to_dict() for item in schemas],
        "snapshot_consistency": manifest["snapshot_consistency"],
        "relation_count": len(schemas),
        "sample_row_counts": {item.relation_id: item.sample_row_count for item in descriptors},
        "population": [item.to_dict() for item in populations],
        "query_count": len(workload.queries),
    }


def load_snapshot(path: Path) -> AdvisorSnapshot:
    root = Path(path).expanduser()
    validate_snapshot(root)
    root = root.resolve()
    manifest = _json_read(root / "manifest.json")
    schemas = _load_schema(_json_read(root / "schema.json"))
    populations = _load_population(_json_read(root / "population.json"))
    workload = _load_workload(_json_read(root / "workload.json"))
    descriptors = _load_descriptors(manifest)
    samples = {
        item.relation_id: deserialize_table(
            _safe_regular_file(root, item.relative_path).read_bytes()
        )
        for item in descriptors
    }
    try:
        dbms = DBMSIdentity(**manifest["dbms"])
    except (TypeError, KeyError) as exc:
        raise SnapshotValidationError("invalid DBMS identity") from exc
    try:
        consistency = SnapshotConsistency(**manifest["snapshot_consistency"])
    except (TypeError, KeyError) as exc:
        raise SnapshotValidationError("invalid snapshot consistency declaration") from exc
    return AdvisorSnapshot(
        schemas,
        populations,
        workload,
        samples,
        dbms,
        consistency,
        manifest.get("semantic_provenance", {}),
        manifest.get("runtime_metadata", {}),
        manifest.get("sensitivity", {}),
        manifest["semantic_digest"],
    )
