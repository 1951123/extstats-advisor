"""Validation and atomic persistence for native PostgreSQL statistics."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import canonical_json, digest_bytes, digest_json
from extstats_advisor.errors import NativeStatsRepositoryValidationError
from extstats_advisor.native_stats.model import (
    ABSENT_NATIVE,
    MATERIALIZATION_METHOD,
    MATERIALIZATION_METHOD_VERSION,
    NATIVE_STATS_REPOSITORY_FORMAT_VERSION,
    ORDINARY_STATS_FINGERPRINT_CONTRACT,
    NativeStatsCandidate,
    NativeStatsMaterialization,
    NativeStatsRepository,
)


def _semantic_manifest(materialization: NativeStatsMaterialization) -> dict[str, Any]:
    return {
        "format_version": NATIVE_STATS_REPOSITORY_FORMAT_VERSION,
        "materialization": {
            "method": MATERIALIZATION_METHOD,
            "method_version": MATERIALIZATION_METHOD_VERSION,
            "analyze_count": materialization.analyze_count,
            "sample_row_count": materialization.sample_row_count,
            "population_row_count": materialization.population_row_count,
            "observed_reltuples": materialization.observed_reltuples,
        },
        "source_snapshot_semantic_digest": materialization.source_snapshot_semantic_digest,
        "candidate_universe_semantic_digest": materialization.candidate_universe_semantic_digest,
        "backend": {
            "contract": materialization.backend_contract,
            "server_version": materialization.server_version,
            "server_version_num": materialization.server_version_num,
            "reference_source_commit": materialization.reference_source_commit,
        },
        "statistics_target": materialization.statistics_target,
        "ordinary_stats": {
            "fingerprint_contract": ORDINARY_STATS_FINGERPRINT_CONTRACT,
            "fingerprint": materialization.ordinary_stats_fingerprint,
        },
        "candidates": [candidate.to_dict() for candidate in materialization.candidates],
    }


def _manifest(materialization: NativeStatsMaterialization) -> dict[str, Any]:
    value = _semantic_manifest(materialization)
    value["semantic_digest"] = digest_json(value)
    if materialization.runtime_metadata:
        value["runtime_metadata"] = dict(materialization.runtime_metadata)
    return value


def write_native_stats_repository(
    materialization: NativeStatsMaterialization, destination: Path
) -> str:
    """Write once, with payload hashes and no overwrite semantics."""

    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"native-stats destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent))
    try:
        payload_dir = temporary / "payloads"
        payload_dir.mkdir()
        manifest = _manifest(materialization)
        for candidate in materialization.candidates:
            payload = materialization.payloads[candidate.candidate_id]
            if candidate.state == ABSENT_NATIVE:
                continue
            path = payload_dir / candidate.payload_path.removeprefix("payloads/")
            path.write_bytes(payload)
        (temporary / "manifest.json").write_bytes(canonical_json(manifest) + b"\n")
        validate_native_stats_repository(temporary)
        os.replace(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return manifest["semantic_digest"]


def _read_manifest(root: Path) -> dict[str, Any]:
    try:
        value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise NativeStatsRepositoryValidationError("invalid native-stats manifest") from exc
    if not isinstance(value, dict):
        raise NativeStatsRepositoryValidationError("native-stats manifest must be an object")
    return value


def _validate_manifest(value: dict[str, Any]) -> None:
    if value.get("format_version") != NATIVE_STATS_REPOSITORY_FORMAT_VERSION:
        raise NativeStatsRepositoryValidationError("unknown native-stats repository format")
    semantic = dict(value)
    semantic.pop("semantic_digest", None)
    semantic.pop("runtime_metadata", None)
    if value.get("semantic_digest") != digest_json(semantic):
        raise NativeStatsRepositoryValidationError("native-stats semantic digest mismatch")
    materialization = value.get("materialization", {})
    if (
        materialization.get("method") != MATERIALIZATION_METHOD
        or materialization.get("analyze_count") != 1
    ):
        raise NativeStatsRepositoryValidationError("invalid native materialization contract")
    if (
        value.get("ordinary_stats", {}).get("fingerprint_contract")
        != ORDINARY_STATS_FINGERPRINT_CONTRACT
    ):
        raise NativeStatsRepositoryValidationError(
            "invalid ordinary statistics fingerprint contract"
        )
    candidates = value.get("candidates")
    if not isinstance(candidates, list):
        raise NativeStatsRepositoryValidationError("candidate inventory must be a list")
    ids = [item.get("candidate_id") for item in candidates if isinstance(item, dict)]
    if len(ids) != len(candidates) or len(ids) != len(set(ids)):
        raise NativeStatsRepositoryValidationError("candidate inventory IDs are invalid")


def validate_native_stats_repository(path: Path) -> dict[str, Any]:
    root = Path(path).expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise NativeStatsRepositoryValidationError("native-stats repository must be a directory")
    manifest = _read_manifest(root)
    _validate_manifest(manifest)
    listed: set[str] = set()
    for item in manifest["candidates"]:
        try:
            candidate = NativeStatsCandidate(
                item["candidate_id"],
                item["relation_id"],
                item["kind"],
                tuple(item["column_ordinals"]),
                tuple(item["column_names"]),
                item["state"],
                item.get("serialization"),
                item["payload_size"],
                item.get("payload_sha256"),
                item.get("payload_path"),
            )
            candidate_id = candidate.candidate_id
            state = candidate.state
            relative = candidate.payload_path
            size = candidate.payload_size
            sha = candidate.payload_sha256
        except (KeyError, TypeError, NativeStatsRepositoryValidationError) as exc:
            raise NativeStatsRepositoryValidationError("invalid candidate inventory entry") from exc
        if state == ABSENT_NATIVE:
            if relative is not None or size != 0 or sha is not None:
                raise NativeStatsRepositoryValidationError("absent candidate has payload metadata")
            continue
        if (
            not isinstance(relative, str)
            or not relative.startswith("payloads/")
            or Path(relative).name != relative.removeprefix("payloads/")
        ):
            raise NativeStatsRepositoryValidationError("invalid payload path")
        if relative in listed:
            raise NativeStatsRepositoryValidationError("duplicate payload path")
        listed.add(relative)
        payload_path = root / relative
        if payload_path.is_symlink() or not payload_path.resolve().is_relative_to(root):
            raise NativeStatsRepositoryValidationError("unsafe native payload path")
        try:
            payload = payload_path.read_bytes()
        except OSError as exc:
            raise NativeStatsRepositoryValidationError("missing native payload") from exc
        if len(payload) != size or digest_bytes(payload) != sha:
            raise NativeStatsRepositoryValidationError(
                f"native payload digest mismatch: {candidate_id}"
            )
    actual = (
        {
            path.relative_to(root).as_posix()
            for path in (root / "payloads").glob("*")
            if path.is_file()
        }
        if (root / "payloads").is_dir()
        else set()
    )
    if actual != listed:
        raise NativeStatsRepositoryValidationError("payload directory does not match manifest")
    return {
        "format_version": manifest["format_version"],
        "semantic_digest": manifest["semantic_digest"],
        "candidate_count": len(manifest["candidates"]),
        "present_count": sum(item["state"] != ABSENT_NATIVE for item in manifest["candidates"]),
    }


def validate_native_stats_repository_compatibility(
    repository: NativeStatsRepository,
    snapshot: Any,
    candidate_universe: Any,
) -> dict[str, Any]:
    """Validate the repository identity and exact candidate inventory for consumers."""

    snapshot_digest = getattr(snapshot, "semantic_digest", None)
    if repository.source_snapshot_semantic_digest != snapshot_digest:
        raise NativeStatsRepositoryValidationError("native repository snapshot digest mismatch")
    if repository.candidate_universe_semantic_digest != candidate_universe.semantic_digest:
        raise NativeStatsRepositoryValidationError(
            "native repository candidate-universe digest mismatch"
        )
    expected = tuple(
        (
            candidate.candidate_id,
            candidate.relation_id,
            candidate.kind,
            candidate.column_ordinals,
            candidate.column_names,
        )
        for candidate in candidate_universe.candidates
    )
    actual = tuple(
        (
            candidate.candidate_id,
            candidate.relation_id,
            candidate.kind,
            candidate.column_ordinals,
            candidate.column_names,
        )
        for candidate in repository.candidate_models
    )
    if actual != expected:
        raise NativeStatsRepositoryValidationError(
            "native repository candidate inventory does not match CandidateUniverse"
        )
    return {
        "semantic_digest": repository.semantic_digest,
        "candidate_count": len(actual),
        "present_count": sum(
            candidate.state == "present" for candidate in repository.candidate_models
        ),
        "absent_count": sum(
            candidate.state == "absent-native" for candidate in repository.candidate_models
        ),
    }


def load_native_stats_repository(path: Path) -> NativeStatsRepository:
    root = Path(path).expanduser().resolve()
    validate_native_stats_repository(root)
    manifest = _read_manifest(root)
    payloads = {}
    for item in manifest["candidates"]:
        if item["payload_path"] is not None:
            payloads[item["candidate_id"]] = (root / item["payload_path"]).read_bytes()
    return NativeStatsRepository(manifest, payloads)


write_postgresql_native_stats_repository = write_native_stats_repository
load_postgresql_native_stats_repository = load_native_stats_repository
validate_postgresql_native_stats_repository = validate_native_stats_repository
