from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import pyarrow as pa

from extstats_advisor.candidates.groups import derive_relevant_groups
from extstats_advisor.candidates.universe import derive_candidate_universe
from extstats_advisor.dbms.postgres.capabilities import expand_postgres_candidates
from extstats_advisor.snapshot.bundle import load_snapshot, write_snapshot
from extstats_advisor.snapshot.model import Workload, WorkloadQuery
from tests.unit.test_snapshot_roundtrip import make_snapshot


def test_three_predicate_columns_create_three_groups_and_six_candidates() -> None:
    snapshot, _ = make_snapshot()
    workload = Workload(
        "three-column-workload",
        (
            WorkloadQuery(
                "q1",
                'SELECT * FROM "Reporting.Schema"."Order Facts" WHERE "Customer ID" = $1 AND amount > $2 AND "客户" IS NOT NULL',
            ),
        ),
    )
    snapshot = replace(snapshot, workload=workload)
    with TemporaryDirectory() as temporary:
        loaded_path = Path(temporary) / "snapshot"
        write_snapshot(snapshot, loaded_path)
        universe = derive_candidate_universe(load_snapshot(loaded_path))
    assert len(universe.relevant_groups) == 3
    assert len(universe.candidates) == 6
    assert len(universe.incidence) == 6


def test_groups_are_schema_sample_independent_and_pair_order_is_canonical() -> None:
    snapshot, table = make_snapshot()
    from extstats_advisor.workload.analysis import PredicateProfile

    profile = PredicateProfile(
        "q1",
        snapshot.schemas[0].relation_id,
        1.0,
        (1, 2, 4),
        ("Customer ID", "amount", "客户"),
        "supported",
    )
    first = derive_relevant_groups(snapshot.schemas[0], (profile,))
    second = derive_relevant_groups(snapshot.schemas[0], (profile,))
    assert first == second
    assert [group.column_ordinals for group in first] == [(1, 2), (1, 4), (2, 4)]
    assert table.num_rows > 0


def test_same_schema_and_workload_with_different_samples_has_same_candidates() -> None:
    snapshot, table = make_snapshot()
    workload = Workload(
        "same-workload",
        (
            WorkloadQuery(
                "q1",
                'SELECT * FROM "Reporting.Schema"."Order Facts" WHERE "Customer ID" = $1 AND amount > $2 AND "客户" IS NOT NULL',
            ),
        ),
    )
    first = replace(snapshot, workload=workload)
    changed_customer_ids = pa.array([99, None, 99], type=pa.int64())
    changed_table = table.set_column(0, table.schema.field(0), changed_customer_ids)
    second = replace(first, samples={"rel_events_01": changed_table})
    with TemporaryDirectory() as temporary:
        first_path = Path(temporary) / "first"
        second_path = Path(temporary) / "second"
        write_snapshot(first, first_path)
        write_snapshot(second, second_path)
        first_universe = derive_candidate_universe(load_snapshot(first_path))
        second_universe = derive_candidate_universe(load_snapshot(second_path))
    assert first_universe.relevant_groups == second_universe.relevant_groups
    assert first_universe.candidates == second_universe.candidates


def test_same_group_across_queries_keeps_structural_evidence() -> None:
    snapshot, _ = make_snapshot()
    from extstats_advisor.workload.analysis import PredicateProfile

    profiles = (
        PredicateProfile(
            "q1", "rel_events_01", 2.0, (1, 2), ("Customer ID", "amount"), "supported"
        ),
        PredicateProfile(
            "q2", "rel_events_01", 3.0, (1, 2), ("Customer ID", "amount"), "supported"
        ),
    )
    groups = derive_relevant_groups(snapshot.schemas[0], profiles)
    assert len(groups) == 1
    assert groups[0].supporting_query_ids == ("q1", "q2")
    assert groups[0].supporting_weight == 5.0


def test_capability_expands_pairs_only_to_mcv_and_dependencies() -> None:
    snapshot, _ = make_snapshot()
    from extstats_advisor.workload.analysis import PredicateProfile

    profiles = (
        PredicateProfile(
            "q1", "rel_events_01", 1.0, (1, 2), ("Customer ID", "amount"), "supported"
        ),
    )
    candidates = expand_postgres_candidates(derive_relevant_groups(snapshot.schemas[0], profiles))
    assert [candidate.kind for candidate in candidates] == [
        "postgresql.mcv",
        "postgresql.dependencies",
    ]
    assert [candidate.static_precedence_rank for candidate in candidates] == [1, 2]
    assert all("score" not in candidate.to_dict() for candidate in candidates)
