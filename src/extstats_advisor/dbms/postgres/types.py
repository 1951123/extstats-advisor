"""Explicit PostgreSQL scalar-to-Arrow type mappings for snapshot v1."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import pyarrow as pa

from extstats_advisor.dbms.postgres.errors import UnsupportedPostgresTypeError

TYPE_MAPPING_CONTRACT_VERSION = "postgresql-arrow-scalars-v1"
_NUMERIC = re.compile(r"^numeric\((\d+),(\-?\d+)\)$")
_VARCHAR = re.compile(r"^(?:character varying|varchar)(?:\(\d+\))?$")
_CHAR = re.compile(r"^(?:character|char)(?:\(\d+\))?$")


@dataclass(frozen=True, slots=True)
class PostgresTypeMapping:
    native_type: str
    arrow_type: pa.DataType
    converter: Callable[[Any], Any]

    @property
    def arrow_type_name(self) -> str:
        return str(self.arrow_type)

    def convert(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            return self.converter(value)
        except (ArithmeticError, AttributeError, TypeError, ValueError, OverflowError) as exc:
            raise UnsupportedPostgresTypeError(
                f"could not convert value for PostgreSQL type {self.native_type!r}"
            ) from exc


def _identity(value: Any) -> Any:
    return value


def _uuid_text(value: Any) -> str:
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return str(UUID(value))
    raise TypeError("UUID value is neither UUID nor text")


def _timestamptz_utc(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TypeError("timestamp with time zone value has no timezone")
    return value.astimezone(UTC)


def _timestamp_naive(value: Any) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is not None:
        raise TypeError("timestamp without time zone value is timezone-aware")
    return value


def _mapping(
    native_type: str, arrow_type: pa.DataType, converter: Callable[[Any], Any]
) -> PostgresTypeMapping:
    return PostgresTypeMapping(native_type, arrow_type, converter)


def map_postgres_type(
    native_type: str, *, typtype: str = "b", typelem: int = 0
) -> PostgresTypeMapping:
    """Return a lossless scalar mapping or fail closed with the native type."""

    if not isinstance(native_type, str) or typtype != "b" or typelem:
        raise UnsupportedPostgresTypeError(
            f"unsupported PostgreSQL type {native_type!r} (domain, enum, or array)"
        )
    if native_type == "boolean":
        return _mapping(native_type, pa.bool_(), _identity)
    if native_type == "smallint":
        return _mapping(native_type, pa.int16(), _identity)
    if native_type == "integer":
        return _mapping(native_type, pa.int32(), _identity)
    if native_type == "bigint":
        return _mapping(native_type, pa.int64(), _identity)
    if native_type == "real":
        return _mapping(native_type, pa.float32(), _identity)
    if native_type == "double precision":
        return _mapping(native_type, pa.float64(), _identity)
    if (
        native_type in {"text", "name"}
        or _VARCHAR.fullmatch(native_type)
        or _CHAR.fullmatch(native_type)
    ):
        return _mapping(native_type, pa.string(), _identity)
    if native_type == "bytea":
        return _mapping(native_type, pa.binary(), _identity)
    if native_type == "date":
        return _mapping(native_type, pa.date32(), _identity)
    if native_type == "timestamp without time zone":
        return _mapping(native_type, pa.timestamp("us"), _timestamp_naive)
    if native_type == "timestamp with time zone":
        return _mapping(native_type, pa.timestamp("us", tz="UTC"), _timestamptz_utc)
    if native_type == "uuid":
        return _mapping(native_type, pa.string(), _uuid_text)
    numeric = _NUMERIC.fullmatch(native_type)
    if numeric:
        precision, scale = (int(part) for part in numeric.groups())
        if precision < 1 or precision > 76 or scale < 0 or scale > precision:
            raise UnsupportedPostgresTypeError(
                f"unsupported or too-wide PostgreSQL type {native_type!r}"
            )
        arrow_type = (
            pa.decimal128(precision, scale) if precision <= 38 else pa.decimal256(precision, scale)
        )
        return _mapping(
            native_type,
            arrow_type,
            lambda value: value if isinstance(value, Decimal) else Decimal(value),
        )
    raise UnsupportedPostgresTypeError(f"unsupported PostgreSQL type {native_type!r}")


def convert_row(row: tuple[Any, ...], mappings: tuple[PostgresTypeMapping, ...]) -> tuple[Any, ...]:
    if len(row) != len(mappings):
        raise UnsupportedPostgresTypeError("PostgreSQL row width does not match extracted schema")
    return tuple(mapping.convert(value) for value, mapping in zip(row, mappings, strict=True))
