from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pyarrow as pa
import pytest

from extstats_advisor.dbms.postgres.errors import UnsupportedPostgresTypeError
from extstats_advisor.dbms.postgres.types import map_postgres_type


@pytest.mark.parametrize(
    ("native", "arrow"),
    [
        ("boolean", "bool"),
        ("smallint", "int16"),
        ("integer", "int32"),
        ("bigint", "int64"),
        ("real", "float"),
        ("double precision", "double"),
        ("text", "string"),
        ("character varying(20)", "string"),
        ("character(4)", "string"),
        ("bytea", "binary"),
        ("date", "date32[day]"),
        ("timestamp without time zone", "timestamp[us]"),
        ("timestamp with time zone", "timestamp[us, tz=UTC]"),
        ("uuid", "string"),
        ("numeric(10,2)", "decimal128(10, 2)"),
        ("numeric(50,4)", "decimal256(50, 4)"),
    ],
)
def test_supported_scalar_type_matrix(native: str, arrow: str) -> None:
    assert str(map_postgres_type(native).arrow_type) == arrow


@pytest.mark.parametrize(
    ("native", "kwargs"),
    [
        ("jsonb", {}),
        ("integer[]", {"typelem": 23}),
        ("status", {"typtype": "e"}),
        ("numeric", {}),
        ("numeric(77,2)", {}),
        ("numeric(10,-1)", {}),
    ],
)
def test_unsupported_types_fail_closed(native: str, kwargs: dict[str, object]) -> None:
    with pytest.raises(UnsupportedPostgresTypeError):
        map_postgres_type(native, **kwargs)


def test_lossless_special_value_converters() -> None:
    timestamptz = map_postgres_type("timestamp with time zone")
    assert timestamptz.convert(datetime(2026, 1, 1, 1, tzinfo=UTC)) == datetime(
        2026, 1, 1, 1, tzinfo=UTC
    )
    uuid_mapping = map_postgres_type("uuid")
    assert uuid_mapping.convert(UUID("00000000-0000-0000-0000-000000000001")) == (
        "00000000-0000-0000-0000-000000000001"
    )
    decimal_mapping = map_postgres_type("numeric(10,2)")
    assert decimal_mapping.convert(Decimal("12.30")) == Decimal("12.30")
    assert decimal_mapping.arrow_type == pa.decimal128(10, 2)
