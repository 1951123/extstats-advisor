"""Portable models for the first PostgreSQL native-statistics artifact."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from extstats_advisor.errors import NativeStatsRepositoryValidationError

NATIVE_STATS_REPOSITORY_FORMAT_VERSION = "postgres-native-stats-repository-v1"
ORDINARY_STATS_FINGERPRINT_CONTRACT = "postgresql-ordinary-stats-fingerprint-v1"
MATERIALIZATION_METHOD = "pg-extstats-frozen-replay-v1"
MATERIALIZATION_METHOD_VERSION = "1"
PRESENT = "present"
ABSENT_NATIVE = "absent-native"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _token(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise NativeStatsRepositoryValidationError(f"{label} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class NativeStatsCandidate:
    candidate_id: str
    relation_id: str
    kind: str
    column_ordinals: tuple[int, ...]
    column_names: tuple[str, ...]
    state: str
    serialization: str | None
    payload_size: int
    payload_sha256: str | None
    payload_path: str | None

    def __post_init__(self) -> None:
        _token(self.candidate_id, "candidate_id")
        _token(self.relation_id, "relation_id")
        if self.kind not in {"postgresql.mcv", "postgresql.dependencies"}:
            raise NativeStatsRepositoryValidationError(
                f"unsupported native statistics kind: {self.kind}"
            )
        if (
            len(self.column_ordinals) != 2
            or tuple(sorted(self.column_ordinals)) != self.column_ordinals
        ):
            raise NativeStatsRepositoryValidationError(
                "native-statistics columns must be a canonical pair"
            )
        if len(self.column_names) != 2 or any(
            not isinstance(name, str) or not name for name in self.column_names
        ):
            raise NativeStatsRepositoryValidationError("native-statistics column names are invalid")
        if self.state not in {PRESENT, ABSENT_NATIVE}:
            raise NativeStatsRepositoryValidationError(
                f"unknown native-statistics state: {self.state}"
            )
        if not isinstance(self.payload_size, int) or self.payload_size < 0:
            raise NativeStatsRepositoryValidationError("payload_size must be non-negative")
        if self.state == PRESENT:
            if (
                not self.serialization
                or self.payload_size < 1
                or self.payload_path != f"payloads/{self.candidate_id}.bin"
            ):
                raise NativeStatsRepositoryValidationError(
                    "present native statistics require a payload"
                )
            if not self.payload_sha256 or not _SHA256.fullmatch(self.payload_sha256):
                raise NativeStatsRepositoryValidationError("present payload digest is invalid")
        elif (
            self.serialization is not None
            or self.payload_size != 0
            or self.payload_sha256 is not None
            or self.payload_path is not None
        ):
            raise NativeStatsRepositoryValidationError(
                "absent native statistics cannot have a payload"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "relation_id": self.relation_id,
            "kind": self.kind,
            "column_ordinals": list(self.column_ordinals),
            "column_names": list(self.column_names),
            "state": self.state,
            "serialization": self.serialization,
            "payload_size": self.payload_size,
            "payload_sha256": self.payload_sha256,
            "payload_path": self.payload_path,
        }


@dataclass(frozen=True, slots=True)
class NativeStatsMaterialization:
    """Runtime result before it is sealed into an artifact directory."""

    source_snapshot_semantic_digest: str
    candidate_universe_semantic_digest: str
    backend_contract: str
    server_version: str
    server_version_num: int
    reference_source_commit: str
    statistics_target: int
    sample_row_count: int
    population_row_count: float
    observed_reltuples: float
    ordinary_stats_fingerprint: str
    candidates: tuple[NativeStatsCandidate, ...]
    payloads: Mapping[str, bytes] = field(default_factory=dict, repr=False, compare=False)
    analyze_count: int = 1
    runtime_metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.source_snapshot_semantic_digest) or not _SHA256.fullmatch(
            self.candidate_universe_semantic_digest
        ):
            raise NativeStatsRepositoryValidationError("source digests must be SHA-256 tokens")
        _token(self.backend_contract, "backend_contract")
        _token(self.server_version, "server_version")
        if not re.fullmatch(r"[0-9a-f]{40}", self.reference_source_commit):
            raise NativeStatsRepositoryValidationError("reference source commit is invalid")
        if self.server_version_num < 1 or self.statistics_target < 1 or self.sample_row_count < 1:
            raise NativeStatsRepositoryValidationError("invalid materialization dimensions")
        if self.analyze_count != 1:
            raise NativeStatsRepositoryValidationError(
                "native materialization must execute exactly one ANALYZE"
            )
        if not _SHA256.fullmatch(self.ordinary_stats_fingerprint):
            raise NativeStatsRepositoryValidationError("ordinary statistics fingerprint is invalid")
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)) or set(ids) != set(self.payloads):
            raise NativeStatsRepositoryValidationError(
                "candidate payload inventory is inconsistent"
            )
        object.__setattr__(self, "payloads", MappingProxyType(dict(self.payloads)))


@dataclass(frozen=True, slots=True)
class NativeStatsRepository:
    """Loaded immutable repository; payload bytes are addressed by opaque candidate ID."""

    manifest: Mapping[str, Any]
    payloads: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest", MappingProxyType(dict(self.manifest)))
        object.__setattr__(self, "payloads", MappingProxyType(dict(self.payloads)))

    @property
    def semantic_digest(self) -> str:
        return str(self.manifest["semantic_digest"])

    @property
    def candidates(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(self.manifest["candidates"])

    @property
    def candidate_models(self) -> tuple[NativeStatsCandidate, ...]:
        return tuple(
            NativeStatsCandidate(
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
            for item in self.manifest["candidates"]
        )

    @property
    def source_snapshot_semantic_digest(self) -> str:
        return str(self.manifest["source_snapshot_semantic_digest"])

    @property
    def candidate_universe_semantic_digest(self) -> str:
        return str(self.manifest["candidate_universe_semantic_digest"])

    @property
    def backend_contract(self) -> str:
        return str(self.manifest["backend"]["contract"])

    @property
    def server_version(self) -> str:
        return str(self.manifest["backend"]["server_version"])

    @property
    def server_version_num(self) -> int:
        return int(self.manifest["backend"]["server_version_num"])

    @property
    def statistics_target(self) -> int:
        return int(self.manifest["statistics_target"])

    @property
    def ordinary_stats_fingerprint(self) -> str:
        return str(self.manifest["ordinary_stats"]["fingerprint"])


# Public name used by the production-facing PostgreSQL backend API.
PostgreSQLNativeStatsRepository = NativeStatsRepository
