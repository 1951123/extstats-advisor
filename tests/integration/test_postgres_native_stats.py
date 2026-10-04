from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pytest

from extstats_advisor.candidates.universe import derive_candidate_universe
from extstats_advisor.dbms.postgres.native_stats import materialize_native_stats
from extstats_advisor.native_stats.repository import (
    load_native_stats_repository,
    validate_native_stats_repository,
    write_native_stats_repository,
)
from extstats_advisor.snapshot.bundle import load_snapshot, write_snapshot
from extstats_advisor.snapshot.model import (
    AdvisorSnapshot,
    ColumnSchema,
    DBMSIdentity,
    PopulationMetadata,
    RelationName,
    RelationSchema,
    Workload,
    WorkloadQuery,
)

pytestmark = pytest.mark.patched_integration


def _snapshot() -> AdvisorSnapshot:
    schema = pa.schema(
        [
            pa.field("a", pa.int32(), nullable=True),
            pa.field("b", pa.string(), nullable=True),
            pa.field("c", pa.string(), nullable=True),
        ]
    )
    table = pa.table(
        {
            "a": pa.array([1, 1, 1, 2, 2, 2, 3, 3] * 3, type=pa.int32()),
            "b": pa.array(["x", "x", "y", "x", "x", "y", "z", "z"] * 3),
            "c": pa.array(["u", "u", "u", "v", "v", "v", "w", "w"] * 3),
        },
        schema=schema,
    )
    relation = RelationSchema(
        "rel_patched_01",
        RelationName("Scratch Table", schema="public"),
        (
            ColumnSchema("a", 1, "int32", True, "integer"),
            ColumnSchema("b", 2, "string", True, "text"),
            ColumnSchema("c", 3, "string", True, "text"),
        ),
    )
    return AdvisorSnapshot(
        (relation,),
        (PopulationMetadata("rel_patched_01", 1000, "estimate", "patched integration fixture"),),
        Workload(
            "patched-native-workload",
            (
                WorkloadQuery(
                    "q1",
                    'SELECT * FROM "public"."Scratch Table" WHERE "a" = 1 AND "b" = 2 AND "c" = 3',
                ),
            ),
        ),
        {"rel_patched_01": table},
        DBMSIdentity("postgresql", "16.14"),
        semantic_provenance={"acquisition": "patched integration fixture"},
    )


def test_patched_postgres_materializes_one_fixed_sample_and_rolls_back(
    patched_postgres_dsn: str, tmp_path: Path
) -> None:
    snapshot_path = tmp_path / "snapshot"
    write_snapshot(_snapshot(), snapshot_path)
    snapshot = load_snapshot(snapshot_path)
    universe = derive_candidate_universe(snapshot)
    assert len(universe.candidates) == 6

    first = materialize_native_stats(
        patched_postgres_dsn, snapshot, universe, statistics_target=100
    )
    second = materialize_native_stats(
        patched_postgres_dsn, snapshot, universe, statistics_target=100
    )
    assert first.analyze_count == 1
    assert first.sample_row_count == 24
    assert first.population_row_count == 1000
    assert first.observed_reltuples == 1000
    assert first.ordinary_stats_fingerprint == second.ordinary_stats_fingerprint
    assert [(item.candidate_id, item.state, item.payload_sha256) for item in first.candidates] == [
        (item.candidate_id, item.state, item.payload_sha256) for item in second.candidates
    ]
    assert any(item.state == "present" for item in first.candidates)
    assert all(item.state in {"present", "absent-native"} for item in first.candidates)

    first_path = tmp_path / "native-first"
    second_path = tmp_path / "native-second"
    write_native_stats_repository(first, first_path)
    write_native_stats_repository(second, second_path)
    assert load_native_stats_repository(first_path).semantic_digest == load_native_stats_repository(
        second_path
    ).semantic_digest
    assert validate_native_stats_repository(first_path)["candidate_count"] == 6

    import psycopg

    with psycopg.connect(patched_postgres_dsn, autocommit=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM pg_catalog.pg_statistic_ext "
            "WHERE stxname LIKE 'extstats_adv_stat_%'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT count(*) FROM pg_catalog.pg_class WHERE relname LIKE 'extstats_adv_target_%'"
        ).fetchone()[0] == 0
