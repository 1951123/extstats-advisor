from __future__ import annotations

import json
from pathlib import Path

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


def test_destination_is_not_overwritten(tmp_path) -> None:
    snapshot, _ = make_snapshot()
    path = tmp_path / "snapshot"
    write_snapshot(snapshot, path)
    with pytest.raises(FileExistsError):
        write_snapshot(snapshot, path)
