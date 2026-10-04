from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow as pa

from extstats_advisor.snapshot.bundle import load_snapshot, validate_snapshot, write_snapshot
from extstats_advisor.snapshot.model import (
    AdvisorSnapshot,
    ColumnSchema,
    DBMSIdentity,
    PopulationMetadata,
    RelationSchema,
    Workload,
    WorkloadQuery,
)


def make_snapshot() -> tuple[AdvisorSnapshot, pa.Table]:
    schema = pa.schema(
        [
            pa.field("i", pa.int64(), nullable=True),
            pa.field("f", pa.float64(), nullable=True),
            pa.field("d", pa.decimal128(10, 2), nullable=True),
            pa.field("s", pa.string(), nullable=True),
            pa.field("b", pa.bool_(), nullable=True),
            pa.field("day", pa.date32(), nullable=True),
            pa.field("ts", pa.timestamp("us"), nullable=True),
            pa.field("tstz", pa.timestamp("us", tz="UTC"), nullable=True),
        ]
    )
    table = pa.Table.from_arrays(
        [
            pa.array([1, None, 1], type=pa.int64()),
            pa.array([1.5, None, 1.5], type=pa.float64()),
            pa.array([Decimal("10.25"), None, Decimal("10.25")], type=pa.decimal128(10, 2)),
            pa.array(["same", None, "same"], type=pa.string()),
            pa.array([True, None, True], type=pa.bool_()),
            pa.array([date(2024, 1, 1), None, date(2024, 1, 1)], type=pa.date32()),
            pa.array(
                [
                    datetime(2024, 1, 1, 12, tzinfo=UTC).replace(tzinfo=None),
                    None,
                    datetime(2024, 1, 1, 12, tzinfo=UTC).replace(tzinfo=None),
                ],
                type=pa.timestamp("us"),
            ),
            pa.array(
                [datetime(2024, 1, 1, 12, tzinfo=UTC), None, datetime(2024, 1, 1, 12, tzinfo=UTC)],
                type=pa.timestamp("us", tz="UTC"),
            ),
        ],
        schema=schema,
    )
    logical = RelationSchema(
        "public.events",
        tuple(
            ColumnSchema(field.name, index + 1, str(field.type), field.nullable)
            for index, field in enumerate(schema)
        ),
    )
    snapshot = AdvisorSnapshot(
        schemas=(logical,),
        populations=(
            PopulationMetadata("public.events", 100000, "estimate", "read-only metadata"),
        ),
        workload=Workload("workload-v1", (WorkloadQuery("q1", "SELECT 1", 1.0),)),
        samples={"public.events": table},
        dbms=DBMSIdentity("example-db", "1"),
        source_provenance={"acquisition": "offline fixture"},
    )
    return snapshot, table


def test_typed_arrow_snapshot_roundtrip_preserves_values_order_nulls_and_duplicates(
    tmp_path,
) -> None:
    snapshot, table = make_snapshot()
    path = tmp_path / "snapshot"
    digest = write_snapshot(snapshot, path)
    summary = validate_snapshot(path)
    loaded = load_snapshot(path)

    assert digest == summary["semantic_digest"]
    assert loaded.samples["public.events"].schema == table.schema
    assert loaded.samples["public.events"].equals(table)
    assert loaded.samples["public.events"].num_rows == 3
    assert loaded.samples["public.events"].column("i").null_count == 1
    assert (
        loaded.samples["public.events"].column("s").to_pylist()[0]
        == loaded.samples["public.events"].column("s").to_pylist()[2]
    )


def test_cli_inspect_is_structural_and_does_not_dump_sample_or_sql(tmp_path, capsys) -> None:
    snapshot, _ = make_snapshot()
    path = tmp_path / "snapshot"
    write_snapshot(snapshot, path)
    from extstats_advisor.cli import main

    assert main(["snapshot", "inspect", str(path)]) == 0
    output = capsys.readouterr().out
    assert "semantic_digest" in output
    assert "SELECT 1" not in output
    assert "same" not in output
