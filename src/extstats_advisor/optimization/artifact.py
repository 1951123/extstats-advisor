"""Validation and atomic persistence for SingletonProfile v1."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from extstats_advisor.canonical import canonical_json
from extstats_advisor.errors import (
    CandidateUniverseValidationError,
    NativeStatsRepositoryValidationError,
    SingletonProfileValidationError,
)
from extstats_advisor.native_stats.model import PRESENT
from extstats_advisor.native_stats.repository import validate_native_stats_repository_compatibility
from extstats_advisor.optimization.singleton import (
    SINGLETON_PROFILE_FORMAT_VERSION,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
)

_TOP_LEVEL_FIELDS = {
    "format_version",
    "source_snapshot_semantic_digest",
    "candidate_universe_semantic_digest",
    "native_stats_repository_semantic_digest",
    "ground_truth_semantic_digest",
    "planner",
    "utility",
    "precedence_policy",
    "baseline",
    "candidate_profiles",
    "frozen_ordered_candidate_ids",
    "semantic_digest",
    "created_at",
    "runtime_metadata",
}


def _read(path: Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file():
        raise SingletonProfileValidationError("singleton profile must be a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SingletonProfileValidationError("invalid singleton profile JSON") from exc
    if not isinstance(value, dict):
        raise SingletonProfileValidationError("singleton profile must be an object")
    return value


def _from_manifest(value: dict[str, Any]) -> SingletonProfile:
    unknown = set(value) - _TOP_LEVEL_FIELDS
    if unknown:
        raise SingletonProfileValidationError("singleton profile contains unknown fields")
    if value.get("format_version") != SINGLETON_PROFILE_FORMAT_VERSION:
        raise SingletonProfileValidationError("unknown singleton profile format")
    planner = value.get("planner")
    utility = value.get("utility")
    baseline = value.get("baseline")
    records = value.get("candidate_profiles")
    if not isinstance(planner, dict) or set(planner) != {
        "sandbox_contract",
        "backend_contract",
        "server_version",
        "server_version_num",
        "ordinary_stats_fingerprint",
    }:
        raise SingletonProfileValidationError("singleton planner manifest is invalid")
    if not isinstance(utility, dict) or set(utility) != {"utility_contract", "loss_contract"}:
        raise SingletonProfileValidationError("singleton utility manifest is invalid")
    if not isinstance(baseline, dict) or set(baseline) != {"objective"}:
        raise SingletonProfileValidationError("singleton baseline manifest is invalid")
    if not isinstance(records, list):
        raise SingletonProfileValidationError("singleton candidate profiles must be a list")
    if not isinstance(value.get("frozen_ordered_candidate_ids"), list):
        raise SingletonProfileValidationError("singleton frozen order must be a list")
    profiles = []
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "candidate_id",
            "native_state",
            "static_precedence_rank",
            "singleton_objective",
            "improvement",
            "frozen_precedence_rank",
        }:
            raise SingletonProfileValidationError("singleton candidate profile is malformed")
        try:
            profiles.append(
                CandidateSingletonProfile(
                    record["candidate_id"],
                    record["native_state"],
                    record["static_precedence_rank"],
                    record["singleton_objective"],
                    record["improvement"],
                    record["frozen_precedence_rank"],
                )
            )
        except (KeyError, TypeError) as exc:
            raise SingletonProfileValidationError(
                "singleton candidate profile is malformed"
            ) from exc
    try:
        profile = SingletonProfile(
            value["source_snapshot_semantic_digest"],
            value["candidate_universe_semantic_digest"],
            value["native_stats_repository_semantic_digest"],
            value["ground_truth_semantic_digest"],
            planner["sandbox_contract"],
            planner["backend_contract"],
            planner["server_version"],
            planner["server_version_num"],
            planner["ordinary_stats_fingerprint"],
            utility["utility_contract"],
            utility["loss_contract"],
            value["precedence_policy"],
            BaselineProfile(baseline["objective"]),
            tuple(profiles),
            tuple(value["frozen_ordered_candidate_ids"]),
            value.get("runtime_metadata", {}),
            value.get("created_at"),
            value.get("semantic_digest"),
        )
    except (KeyError, TypeError) as exc:
        raise SingletonProfileValidationError("singleton profile is incomplete") from exc
    if profile.semantic_digest != profile.computed_semantic_digest:
        raise SingletonProfileValidationError("singleton profile semantic digest mismatch")
    return profile


def _ground_truth_digest(ground_truth: Any) -> str:
    digest = getattr(ground_truth, "semantic_digest", None)
    if isinstance(digest, str):
        return digest
    return ground_truth.computed_semantic_digest


def _validate_bindings(
    profile: SingletonProfile,
    snapshot: Any | None,
    candidate_universe: Any | None,
    native_repository: Any | None,
    ground_truth: Any | None,
) -> None:
    if snapshot is not None and profile.source_snapshot_semantic_digest != snapshot.semantic_digest:
        raise SingletonProfileValidationError("singleton snapshot digest mismatch")
    if (
        candidate_universe is not None
        and profile.candidate_universe_semantic_digest != candidate_universe.semantic_digest
    ):
        raise SingletonProfileValidationError("singleton candidate universe digest mismatch")
    if (
        native_repository is not None
        and profile.native_stats_repository_semantic_digest != native_repository.semantic_digest
    ):
        raise SingletonProfileValidationError("singleton native repository digest mismatch")
    if ground_truth is not None and profile.ground_truth_semantic_digest != _ground_truth_digest(
        ground_truth
    ):
        raise SingletonProfileValidationError("singleton ground truth digest mismatch")
    if candidate_universe is not None:
        expected_ids = {candidate.candidate_id for candidate in candidate_universe.candidates}
        actual_ids = {candidate.candidate_id for candidate in profile.candidate_profiles}
        if actual_ids != expected_ids:
            raise SingletonProfileValidationError("singleton candidate set does not match universe")
        by_id = {candidate.candidate_id: candidate for candidate in candidate_universe.candidates}
        for record in profile.candidate_profiles:
            candidate = by_id[record.candidate_id]
            if record.static_precedence_rank != candidate.static_precedence_rank:
                raise SingletonProfileValidationError(
                    f"singleton static precedence rank mismatch: {record.candidate_id}"
                )
    if native_repository is not None:
        repository_by_id = {
            candidate.candidate_id: candidate for candidate in native_repository.candidate_models
        }
        profile_by_id = {record.candidate_id: record for record in profile.candidate_profiles}
        if set(repository_by_id) != set(profile_by_id):
            raise SingletonProfileValidationError(
                "singleton candidate set does not match repository"
            )
        for candidate_id, native in repository_by_id.items():
            if profile_by_id[candidate_id].native_state != native.state:
                raise SingletonProfileValidationError(
                    f"singleton native state mismatch: {candidate_id}"
                )
    if snapshot is not None and candidate_universe is not None and native_repository is not None:
        try:
            validate_native_stats_repository_compatibility(
                native_repository, snapshot, candidate_universe
            )
        except (NativeStatsRepositoryValidationError, CandidateUniverseValidationError) as exc:
            raise SingletonProfileValidationError(
                "singleton source artifacts are incompatible"
            ) from exc


def load_singleton_profile(path: Path) -> SingletonProfile:
    return _from_manifest(_read(Path(path)))


def validate_singleton_profile(
    path: Path,
    snapshot: Any | None = None,
    candidate_universe: Any | None = None,
    native_repository: Any | None = None,
    ground_truth: Any | None = None,
) -> dict[str, Any]:
    profile = load_singleton_profile(path)
    _validate_bindings(profile, snapshot, candidate_universe, native_repository, ground_truth)
    return singleton_profile_summary(profile)


def write_singleton_profile(profile: SingletonProfile, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"singleton profile destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(canonical_json(profile.to_manifest()) + b"\n")
        validate_singleton_profile(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return profile.computed_semantic_digest


def singleton_profile_summary(profile: SingletonProfile) -> dict[str, Any]:
    present = [item for item in profile.candidate_profiles if item.native_state == PRESENT]
    improving = sum(item.improvement > 0 for item in present)
    neutral = sum(item.improvement == 0 for item in present)
    worsening = sum(item.improvement < 0 for item in present)
    best = min(
        present, key=lambda item: (item.singleton_objective, item.candidate_id), default=None
    )
    return {
        "format_version": SINGLETON_PROFILE_FORMAT_VERSION,
        "semantic_digest": profile.computed_semantic_digest,
        "source_snapshot_semantic_digest": profile.source_snapshot_semantic_digest,
        "candidate_count": len(profile.candidate_profiles),
        "present_count": profile.present_count,
        "absent_count": profile.absent_count,
        "baseline_objective": profile.baseline.objective,
        "improving_singleton_count": improving,
        "neutral_singleton_count": neutral,
        "worsening_singleton_count": worsening,
        "best_singleton_candidate_id": None if best is None else best.candidate_id,
        "best_singleton_objective": None if best is None else best.singleton_objective,
        "best_singleton_improvement": None if best is None else best.improvement,
        "frozen_ordered_candidate_ids": list(profile.frozen_ordered_candidate_ids),
        "utility_contract": profile.utility_contract,
        "loss_contract": profile.loss_contract,
        "precedence_policy": profile.precedence_policy,
        "baseline_configuration_count": profile.runtime_metadata.get(
            "baseline_configuration_count"
        ),
        "singleton_configuration_count": profile.runtime_metadata.get(
            "singleton_configuration_count"
        ),
        "planner_query_estimate_count": profile.runtime_metadata.get(
            "planner_query_estimate_count"
        ),
    }


def inspect_singleton_profile(path: Path) -> dict[str, Any]:
    """Return the deterministic diagnostic summary for an artifact."""

    return singleton_profile_summary(load_singleton_profile(path))
