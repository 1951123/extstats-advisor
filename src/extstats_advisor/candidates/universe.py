"""CandidateUniverse v1 derivation and sealed single-file artifact operations."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from extstats_advisor.candidates.groups import RelevantGroup, derive_relevant_groups
from extstats_advisor.candidates.model import Candidate, Incidence
from extstats_advisor.canonical import canonical_json, digest_json
from extstats_advisor.dbms.postgres.capabilities import (
    POSTGRES_CANDIDATE_KINDS,
    POSTGRES_CAPABILITY_VERSION,
    POSTGRES_STATIC_PRECEDENCE_VERSION,
    PostgresStatisticsCapability,
    static_precedence_key,
)
from extstats_advisor.dbms.postgres.workload import analyze_workload, parser_metadata
from extstats_advisor.errors import CandidateGenerationError, CandidateUniverseValidationError
from extstats_advisor.snapshot.model import AdvisorSnapshot
from extstats_advisor.workload.analysis import PredicateProfile

CANDIDATE_UNIVERSE_FORMAT_VERSION = "candidate-universe-v1"


def _semantic_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in value if key != "semantic_digest"}


def _digest(value: Mapping[str, Any]) -> str:
    return digest_json(_semantic_payload(value))


def _incidence(
    profiles: Sequence[PredicateProfile],
    groups: Sequence[RelevantGroup],
    candidates: Sequence[Candidate],
) -> tuple[Incidence, ...]:
    profile_by_query = {profile.query_id: profile for profile in profiles}
    group_by_id = {group.group_id: group for group in groups}
    result = []
    for candidate in candidates:
        group = group_by_id[candidate.group_id]
        required = set(group.column_ordinals)
        for query_id in sorted(profile_by_query):
            profile = profile_by_query[query_id]
            if (
                profile.analysis_status == "supported"
                and profile.relation_id == candidate.relation_id
                and required.issubset(profile.predicate_column_ordinals)
            ):
                result.append(Incidence(query_id, candidate.candidate_id))
    return tuple(sorted(result, key=lambda item: (item.query_id, item.candidate_id)))


class CandidateUniverse:
    """Deterministic structural candidates derived from one sealed snapshot."""

    def __init__(
        self,
        source_snapshot_semantic_digest: str,
        workload_analysis: Mapping[str, Any],
        capability: Mapping[str, Any],
        static_precedence_policy: str,
        query_profiles: tuple[PredicateProfile, ...],
        relevant_groups: tuple[RelevantGroup, ...],
        candidates: tuple[Candidate, ...],
        incidence: tuple[Incidence, ...],
    ) -> None:
        self.source_snapshot_semantic_digest = source_snapshot_semantic_digest
        self.workload_analysis = dict(workload_analysis)
        self.capability = dict(capability)
        self.static_precedence_policy = static_precedence_policy
        self.query_profiles = query_profiles
        self.relevant_groups = relevant_groups
        self.candidates = candidates
        self.incidence = incidence
        self._validate()

    def _validate(self) -> None:
        if len(self.source_snapshot_semantic_digest) != 64 or any(
            char not in "0123456789abcdef" for char in self.source_snapshot_semantic_digest
        ):
            raise CandidateUniverseValidationError("invalid source snapshot semantic digest")
        if (
            self.workload_analysis.get("parser") != "pglast"
            or not isinstance(self.workload_analysis.get("parser_version"), str)
            or self.workload_analysis.get("analysis_contract_version")
            != "postgresql-simple-selection-v1"
        ):
            raise CandidateUniverseValidationError("invalid workload analysis metadata")
        if self.static_precedence_policy != POSTGRES_STATIC_PRECEDENCE_VERSION:
            raise CandidateUniverseValidationError("unsupported static precedence policy")
        if self.capability.get("capability_version") != POSTGRES_CAPABILITY_VERSION:
            raise CandidateUniverseValidationError("unsupported candidate capability version")
        if tuple(self.capability.get("supported_kinds", ())) != POSTGRES_CANDIDATE_KINDS:
            raise CandidateUniverseValidationError("unsupported PostgreSQL capability kinds")
        profile_ids = [profile.query_id for profile in self.query_profiles]
        group_ids = [group.group_id for group in self.relevant_groups]
        candidate_ids = [candidate.candidate_id for candidate in self.candidates]
        if len(profile_ids) != len(set(profile_ids)):
            raise CandidateUniverseValidationError("duplicate query profile IDs")
        if len(group_ids) != len(set(group_ids)):
            raise CandidateUniverseValidationError("duplicate relevant group IDs")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise CandidateUniverseValidationError("duplicate candidate IDs")
        if [candidate.static_precedence_rank for candidate in self.candidates] != list(
            range(1, len(self.candidates) + 1)
        ):
            raise CandidateUniverseValidationError("candidate precedence ranks are not contiguous")
        groups = {group.group_id: group for group in self.relevant_groups}
        candidates = {candidate.candidate_id: candidate for candidate in self.candidates}
        queries = set(profile_ids)
        for group in self.relevant_groups:
            if len(group.column_ordinals) != 2:
                raise CandidateUniverseValidationError("candidate capability requires pair groups")
            if not set(group.supporting_query_ids).issubset(queries):
                raise CandidateUniverseValidationError("group references unknown supporting query")
        for candidate in self.candidates:
            if candidate.group_id not in groups:
                raise CandidateUniverseValidationError("candidate references unknown group")
            if candidate.kind not in POSTGRES_CANDIDATE_KINDS:
                raise CandidateUniverseValidationError(
                    "candidate kind is not in declared capability"
                )
            group = groups[candidate.group_id]
            if (
                candidate.relation_id != group.relation_id
                or candidate.column_ordinals != group.column_ordinals
                or candidate.column_names != group.column_names
            ):
                raise CandidateUniverseValidationError("candidate does not match its group")
        if tuple(sorted(self.candidates, key=static_precedence_key)) != self.candidates:
            raise CandidateUniverseValidationError("candidates are not in static precedence order")
        seen_incidence: set[tuple[str, str]] = set()
        for item in self.incidence:
            key = (item.query_id, item.candidate_id)
            if (
                key in seen_incidence
                or item.query_id not in queries
                or item.candidate_id not in candidates
            ):
                raise CandidateUniverseValidationError("invalid or duplicate incidence")
            seen_incidence.add(key)

    def candidate_ids_for_query(self, query_id: str) -> tuple[str, ...]:
        return tuple(item.candidate_id for item in self.incidence if item.query_id == query_id)

    def query_ids_for_candidate(self, candidate_id: str) -> tuple[str, ...]:
        return tuple(item.query_id for item in self.incidence if item.candidate_id == candidate_id)

    def to_dict(self) -> dict[str, Any]:
        value = {
            "format_version": CANDIDATE_UNIVERSE_FORMAT_VERSION,
            "source_snapshot_semantic_digest": self.source_snapshot_semantic_digest,
            "workload_analysis": self.workload_analysis,
            "capability": self.capability,
            "static_precedence_policy": self.static_precedence_policy,
            "query_profiles": [profile.to_dict() for profile in self.query_profiles],
            "relevant_groups": [group.to_dict() for group in self.relevant_groups],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "incidence": [item.to_dict() for item in self.incidence],
        }
        value["semantic_digest"] = _digest(value)
        return value

    @property
    def semantic_digest(self) -> str:
        return self.to_dict()["semantic_digest"]


def derive_candidate_universe(snapshot: AdvisorSnapshot) -> CandidateUniverse:
    if len(snapshot.schemas) != 1:
        raise CandidateGenerationError(
            "candidate generation v1 requires exactly one snapshot relation"
        )
    schema = snapshot.schemas[0]
    profiles = analyze_workload(snapshot.workload, schema)
    groups = derive_relevant_groups(schema, profiles)
    capability = PostgresStatisticsCapability()
    candidates = capability.expand(groups)
    return CandidateUniverse(
        validate_snapshot_semantic_digest(snapshot),
        parser_metadata(),
        capability.to_dict(),
        POSTGRES_STATIC_PRECEDENCE_VERSION,
        profiles,
        groups,
        candidates,
        _incidence(profiles, groups, candidates),
    )


def validate_snapshot_semantic_digest(snapshot: AdvisorSnapshot) -> str:
    """Return the already-sealed source digest without connecting to PostgreSQL."""

    marker = getattr(snapshot, "semantic_digest", None)
    if isinstance(marker, str) and len(marker) == 64:
        return marker
    raise CandidateGenerationError(
        "candidate derivation requires a loaded sealed snapshot semantic digest"
    )


def write_candidate_universe(universe: CandidateUniverse, destination: Path) -> str:
    destination = Path(destination).expanduser().resolve()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"candidate-universe destination already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = canonical_json(universe.to_dict()) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, destination)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return universe.to_dict()["semantic_digest"]


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CandidateUniverseValidationError("invalid candidate-universe JSON") from exc
    if not isinstance(value, dict):
        raise CandidateUniverseValidationError("candidate-universe JSON must be an object")
    return value


def _from_dict(value: dict[str, Any]) -> CandidateUniverse:
    if value.get("format_version") != CANDIDATE_UNIVERSE_FORMAT_VERSION:
        raise CandidateUniverseValidationError("unknown candidate-universe format")
    try:
        profiles = tuple(PredicateProfile.from_dict(item) for item in value["query_profiles"])
        groups = tuple(RelevantGroup.from_dict(item) for item in value["relevant_groups"])
        candidates = tuple(Candidate.from_dict(item) for item in value["candidates"])
        incidence = tuple(Incidence.from_dict(item) for item in value["incidence"])
        universe = CandidateUniverse(
            value["source_snapshot_semantic_digest"],
            value["workload_analysis"],
            value["capability"],
            value["static_precedence_policy"],
            profiles,
            groups,
            candidates,
            incidence,
        )
    except (CandidateGenerationError, KeyError, TypeError) as exc:
        raise CandidateUniverseValidationError(
            "candidate-universe is missing required fields"
        ) from exc
    if value.get("semantic_digest") != _digest(value):
        raise CandidateUniverseValidationError("candidate-universe semantic digest mismatch")
    return universe


def validate_candidate_universe(
    path: Path, snapshot: AdvisorSnapshot | None = None
) -> dict[str, Any]:
    value = _read(Path(path).expanduser())
    universe = _from_dict(value)
    if snapshot is not None:
        expected = validate_snapshot_semantic_digest(snapshot)
        if universe.source_snapshot_semantic_digest != expected:
            raise CandidateUniverseValidationError("source snapshot semantic digest mismatch")
        relation_ids = {schema.relation_id for schema in snapshot.schemas}
        workload_queries = {query.query_id: query for query in snapshot.workload.queries}
        for profile in universe.query_profiles:
            if profile.query_id not in workload_queries:
                raise CandidateUniverseValidationError("profile references unknown workload query")
            if profile.weight != workload_queries[profile.query_id].weight:
                raise CandidateUniverseValidationError("profile workload weight mismatch")
            if profile.relation_id is not None and profile.relation_id not in relation_ids:
                raise CandidateUniverseValidationError("profile references unknown relation")
        for group in universe.relevant_groups:
            if group.relation_id not in relation_ids:
                raise CandidateUniverseValidationError("group references unknown relation")
            schema = next(
                schema for schema in snapshot.schemas if schema.relation_id == group.relation_id
            )
            ordinals = {column.ordinal for column in schema.columns}
            if not set(group.column_ordinals).issubset(ordinals):
                raise CandidateUniverseValidationError("group references unknown column ordinal")
            names = tuple(
                next(column.name for column in schema.columns if column.ordinal == ordinal)
                for ordinal in group.column_ordinals
            )
            if names != group.column_names:
                raise CandidateUniverseValidationError("group column names do not match snapshot")
        for candidate in universe.candidates:
            schema = next(
                schema for schema in snapshot.schemas if schema.relation_id == candidate.relation_id
            )
            ordinals = {column.ordinal for column in schema.columns}
            if not set(candidate.column_ordinals).issubset(ordinals):
                raise CandidateUniverseValidationError(
                    "candidate references unknown column ordinal"
                )
            names = tuple(
                next(column.name for column in schema.columns if column.ordinal == ordinal)
                for ordinal in candidate.column_ordinals
            )
            if names != candidate.column_names:
                raise CandidateUniverseValidationError(
                    "candidate column names do not match snapshot"
                )
    return {
        "format_version": CANDIDATE_UNIVERSE_FORMAT_VERSION,
        "semantic_digest": value["semantic_digest"],
        "source_snapshot_semantic_digest": universe.source_snapshot_semantic_digest,
        "query_count": len(universe.query_profiles),
        "supported_query_count": sum(
            profile.analysis_status == "supported" for profile in universe.query_profiles
        ),
        "relevant_group_count": len(universe.relevant_groups),
        "candidate_count": len(universe.candidates),
        "incidence_count": len(universe.incidence),
        "analysis_contract_version": universe.workload_analysis["analysis_contract_version"],
        "capability_version": universe.capability["capability_version"],
        "static_precedence_policy": universe.static_precedence_policy,
    }


def load_candidate_universe(
    path: Path, snapshot: AdvisorSnapshot | None = None
) -> CandidateUniverse:
    value = _read(Path(path).expanduser())
    universe = _from_dict(value)
    if snapshot is not None:
        validate_candidate_universe(path, snapshot)
    return universe
