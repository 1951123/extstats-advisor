from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pytest

from extstats_advisor.errors import SnapshotValidationError
from extstats_advisor.snapshot.bundle import validate_snapshot, write_snapshot
from tests.unit.test_snapshot_roundtrip import make_snapshot


def published_snapshot(tmp_path: Path) -> Path:
    snapshot, _ = make_snapshot()
    path = tmp_path / "snapshot"
    write_snapshot(snapshot, path)
    return path


@pytest.mark.parametrize("component", ["schema.json", "population.json", "workload.json"])
def test_json_component_mutations_are_rejected(tmp_path, component) -> None:
    path = published_snapshot(tmp_path)
    source = path / component
    value = json.loads(source.read_text())
    if component == "schema.json":
        value["relations"][0]["columns"][0]["nullable"] = False
    elif component == "population.json":
        value["relations"][0]["row_count"] = 101
    else:
        value["queries"][0]["sql"] = "SELECT 2"
    source.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_sample_bytes_are_rejected(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    sample = next((path / "samples").iterdir())
    payload = bytearray(sample.read_bytes())
    payload[20] ^= 1
    sample.write_bytes(payload)
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_manifest_root_digest_mutation_is_rejected(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["semantic_digest"] = "0" * 64
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_unknown_version_unsealed_and_path_traversal_are_rejected(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["format_version"] = "advisor-snapshot-v999"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)

    path = published_snapshot(tmp_path / "second")
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sealed"] = False
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_parent_path_traversal_is_rejected_independently(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sample_inventory"][0]["relative_path"] = "../outside.arrow"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_absolute_sample_path_is_rejected_independently(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sample_inventory"][0]["relative_path"] = "/tmp/sample.arrow"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_symlinked_sample_payload_is_rejected_independently(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    sample = next((path / "samples").iterdir())
    backup = tmp_path / "sample-backup.arrow"
    backup.write_bytes(sample.read_bytes())
    sample.unlink()
    sample.symlink_to(backup)
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_symlinked_required_json_component_is_rejected_independently(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    source = path / "schema.json"
    backup = tmp_path / "schema-backup.json"
    backup.write_bytes(source.read_bytes())
    source.unlink()
    source.symlink_to(backup)
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_dbms_identity_tampering_is_rejected_by_root_identity(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["dbms"]["name"] = "tampered-dbms"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_snapshot_consistency_tampering_is_rejected_by_root_identity(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["snapshot_consistency"]["workload_source"] = "source-view"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_nonsemantic_runtime_fields_do_not_change_root_identity(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    original = value["semantic_digest"]
    value["created_at"] = "2099-01-01T00:00:00Z"
    value["runtime_metadata"] = {"writer_pid": 12345}
    manifest.write_text(json.dumps(value), encoding="utf-8")
    assert validate_snapshot(path)["semantic_digest"] == original


def test_sensitivity_tampering_is_rejected_by_root_identity(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sensitivity"]["anonymized_or_encrypted_by_this_repository"] = True
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_sample_descriptor_tampering_is_rejected_independently(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sample_inventory"][0]["payload_sha256"] = "0" * 64
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_duplicate_sample_relation_references_are_rejected_independently(tmp_path) -> None:
    path = published_snapshot(tmp_path)
    manifest = path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["sample_inventory"].append(dict(value["sample_inventory"][0]))
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(SnapshotValidationError):
        validate_snapshot(path)


def test_zero_row_sample_is_rejected(tmp_path) -> None:
    snapshot, table = make_snapshot()
    empty = pa.Table.from_arrays(
        [column.slice(0, 0) for column in table.columns], schema=table.schema
    )
    with pytest.raises(SnapshotValidationError):
        write_snapshot(replace(snapshot, samples={"rel_events_01": empty}), tmp_path / "zero-row")


def test_all_zero_workload_is_rejected() -> None:
    from extstats_advisor.snapshot.model import Workload, WorkloadQuery

    with pytest.raises(SnapshotValidationError):
        Workload("zero-workload", (WorkloadQuery("q1", "SELECT 1", 0.0),))


def test_destination_is_not_overwritten(tmp_path) -> None:
    snapshot, _ = make_snapshot()
    path = tmp_path / "snapshot"
    write_snapshot(snapshot, path)
    with pytest.raises(FileExistsError):
        write_snapshot(snapshot, path)
