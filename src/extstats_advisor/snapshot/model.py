"""Portable, validated AdvisorSnapshot v1 data model."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from extstats_advisor.errors import SnapshotValidationError

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SnapshotValidationError(f"{label} must be a non-empty string")
    return value


def _identifier(value: Any, label: str) -> str:
    value = _text(value, label)
    if not _IDENTIFIER.fullmatch(value):
        raise SnapshotValidationError(f"{label} is not a portable identifier: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class DBMSIdentity:
    name: str
    version: str | None = None

    def __post_init__(self) -> None:
        _text(self.name, "DBMS name")
        if self.version is not None:
            _text(self.version, "DBMS version")

    def to_dict(self) -> dict[str, Any]:
        value = {"name": self.name}
        if self.version is not None:
            value["version"] = self.version
        return value


@dataclass(frozen=True, slots=True)
class ColumnSchema:
    name: str
    ordinal: int
    arrow_type: str
    nullable: bool
    native_type: str | None = None
    native_collation: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.name, "column name")
        if not isinstance(self.ordinal, int) or self.ordinal < 1:
            raise SnapshotValidationError("column ordinal must be positive")
        _text(self.arrow_type, "Arrow type")
        if not isinstance(self.nullable, bool):
            raise SnapshotValidationError("column nullable must be boolean")
        for value, label in (
            (self.native_type, "native type"),
            (self.native_collation, "native collation"),
        ):
            if value is not None:
                _text(value, label)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": self.name,
            "ordinal": self.ordinal,
            "arrow_type": self.arrow_type,
            "nullable": self.nullable,
        }
        if self.native_type is not None:
            value["native_type"] = self.native_type
        if self.native_collation is not None:
            value["native_collation"] = self.native_collation
        return value


@dataclass(frozen=True, slots=True)
class RelationSchema:
    relation_id: str
    columns: tuple[ColumnSchema, ...]

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "relation ID")
        if not self.columns:
            raise SnapshotValidationError(f"relation has no columns: {self.relation_id}")
        names = [column.name for column in self.columns]
        ordinals = [column.ordinal for column in self.columns]
        if len(names) != len(set(names)) or len(ordinals) != len(set(ordinals)):
            raise SnapshotValidationError(f"relation columns are not unique: {self.relation_id}")
        if ordinals != list(range(1, len(ordinals) + 1)):
            raise SnapshotValidationError(
                f"relation column ordinals are not contiguous: {self.relation_id}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "columns": [column.to_dict() for column in self.columns],
        }


@dataclass(frozen=True, slots=True)
class PopulationMetadata:
    relation_id: str
    row_count: float
    row_count_quality: str
    row_count_source: str

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "population relation ID")
        if (
            not isinstance(self.row_count, (int, float))
            or isinstance(self.row_count, bool)
            or not math.isfinite(float(self.row_count))
            or float(self.row_count) <= 0
        ):
            raise SnapshotValidationError("population row_count must be positive and finite")
        if self.row_count_quality not in {"exact", "estimate"}:
            raise SnapshotValidationError("row_count_quality must be exact or estimate")
        _text(self.row_count_source, "population row_count_source")

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "row_count": self.row_count,
            "row_count_quality": self.row_count_quality,
            "row_count_source": self.row_count_source,
        }


@dataclass(frozen=True, slots=True)
class WorkloadQuery:
    query_id: str
    sql: str
    weight: float = 1.0

    def __post_init__(self) -> None:
        _identifier(self.query_id, "query ID")
        if not isinstance(self.sql, str) or not self.sql.strip():
            raise SnapshotValidationError("query SQL must be non-empty")
        if (
            not isinstance(self.weight, (int, float))
            or isinstance(self.weight, bool)
            or not math.isfinite(float(self.weight))
            or float(self.weight) < 0
        ):
            raise SnapshotValidationError("query weight must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {"query_id": self.query_id, "sql": self.sql, "weight": self.weight}


@dataclass(frozen=True, slots=True)
class Workload:
    workload_id: str
    queries: tuple[WorkloadQuery, ...]
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.workload_id, "workload ID")
        if not self.queries:
            raise SnapshotValidationError("workload must contain at least one query")
        ids = [query.query_id for query in self.queries]
        if len(ids) != len(set(ids)):
            raise SnapshotValidationError("workload query IDs must be unique")

    def to_dict(self) -> dict[str, Any]:
        value = {
            "workload_id": self.workload_id,
            "queries": [query.to_dict() for query in self.queries],
        }
        if self.provenance:
            value["provenance"] = dict(self.provenance)
        return value


@dataclass(frozen=True, slots=True)
class SampleDescriptor:
    relation_id: str
    relative_path: str
    sample_row_count: int
    serialization: str
    payload_sha256: str
    writer_provenance: Mapping[str, Any]
    row_order_semantics: str = "preserved"

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "sample relation ID")
        _text(self.relative_path, "sample relative path")
        if not isinstance(self.sample_row_count, int) or self.sample_row_count < 0:
            raise SnapshotValidationError("sample_row_count must be non-negative")
        if self.serialization != "arrow-ipc-file" or not re.fullmatch(
            r"[0-9a-f]{64}", self.payload_sha256
        ):
            raise SnapshotValidationError("invalid sample serialization or payload digest")
        if self.row_order_semantics != "preserved":
            raise SnapshotValidationError("sample row order must be preserved")

    def to_dict(self) -> dict[str, Any]:
        return {
            "relation_id": self.relation_id,
            "relative_path": self.relative_path,
            "sample_row_count": self.sample_row_count,
            "serialization": self.serialization,
            "payload_sha256": self.payload_sha256,
            "writer_provenance": dict(self.writer_provenance),
            "row_order_semantics": self.row_order_semantics,
        }


@dataclass(frozen=True, slots=True)
class AdvisorSnapshot:
    schemas: tuple[RelationSchema, ...]
    populations: tuple[PopulationMetadata, ...]
    workload: Workload
    samples: Mapping[str, Any]
    dbms: DBMSIdentity
    source_provenance: Mapping[str, Any] = field(default_factory=dict)
    sensitivity: Mapping[str, Any] = field(
        default_factory=lambda: {
            "contains_production_sensitive_values": True,
            "anonymized_or_encrypted_by_this_repository": False,
        }
    )

    def __post_init__(self) -> None:
        relation_ids = [schema.relation_id for schema in self.schemas]
        population_ids = [item.relation_id for item in self.populations]
        if not relation_ids or len(relation_ids) != len(set(relation_ids)):
            raise SnapshotValidationError("snapshot relation IDs must be unique and non-empty")
        if len(population_ids) != len(set(population_ids)) or set(population_ids) != set(
            relation_ids
        ):
            raise SnapshotValidationError(
                "population metadata must cover exactly the schema relations"
            )
        if set(self.samples) != set(relation_ids):
            raise SnapshotValidationError("samples must cover exactly the schema relations")

    @property
    def schema_by_id(self) -> dict[str, RelationSchema]:
        return {item.relation_id: item for item in self.schemas}
