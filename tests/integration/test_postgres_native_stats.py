from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pyarrow as pa
import pytest

from extstats_advisor.candidates.universe import derive_candidate_universe
from extstats_advisor.dbms.postgres.native_stats import materialize_native_stats
from extstats_advisor.dbms.postgres.planner import (
    PostgresPlannerSession,
    PostgresStatisticsConfiguration,
)
from extstats_advisor.dbms.postgres.profiling import profile_postgres_singletons
from extstats_advisor.dbms.postgres.sandbox import (
    destroy_postgres_planner_sandbox,
    prepare_postgres_planner_sandbox,
    verify_postgres_planner_sandbox,
)
from extstats_advisor.errors import PlannerSandboxValidationError
from extstats_advisor.ground_truth import (
    PRODUCTION_EXACT_SOURCE,
    ArtifactGroundTruthProvider,
    CardinalityTruth,
    GroundTruthSet,
    GroundTruthSource,
    load_ground_truth_set,
    write_ground_truth_set,
)
from extstats_advisor.native_stats.model import ABSENT_NATIVE, NativeStatsCandidate
from extstats_advisor.native_stats.repository import (
    load_native_stats_repository,
    validate_native_stats_repository,
    write_native_stats_repository,
)
from extstats_advisor.optimization.artifact import (
    load_singleton_profile,
    validate_singleton_profile,
    write_singleton_profile,
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
from extstats_advisor.utility import QErrorLoss, WeightedWorkloadUtility

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
        RelationName("Scratch Table", schema="public", catalog="postgres"),
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
                    'SELECT * FROM "public"."Scratch Table" '
                    'WHERE "a" = 1 AND "b" = \'x\' AND "c" = \'u\'',
                ),
            ),
        ),
        {"rel_patched_01": table},
        DBMSIdentity("postgresql", "16.14"),
        semantic_provenance={
            "acquisition": "patched integration fixture",
            "source_view_token": "patched-fixture-source-view",
        },
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
    assert (
        load_native_stats_repository(first_path).semantic_digest
        == load_native_stats_repository(second_path).semantic_digest
    )
    assert validate_native_stats_repository(first_path)["candidate_count"] == 6

    import psycopg

    with psycopg.connect(patched_postgres_dsn, autocommit=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_catalog.pg_statistic_ext "
                "WHERE stxname LIKE 'extstats_adv_stat_%'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT count(*) FROM pg_catalog.pg_class WHERE relname LIKE 'extstats_adv_target_%'"
            ).fetchone()[0]
            == 0
        )


def _repository_with_absent_candidate(materialization):
    absent_id = next(
        candidate.candidate_id
        for candidate in materialization.candidates
        if candidate.kind == "postgresql.dependencies"
    )
    candidates = tuple(
        NativeStatsCandidate(
            candidate.candidate_id,
            candidate.relation_id,
            candidate.kind,
            candidate.column_ordinals,
            candidate.column_names,
            ABSENT_NATIVE if candidate.candidate_id == absent_id else candidate.state,
            None if candidate.candidate_id == absent_id else candidate.serialization,
            0 if candidate.candidate_id == absent_id else candidate.payload_size,
            None if candidate.candidate_id == absent_id else candidate.payload_sha256,
            None if candidate.candidate_id == absent_id else candidate.payload_path,
        )
        for candidate in materialization.candidates
    )
    payloads = dict(materialization.payloads)
    payloads[absent_id] = b""
    return replace(materialization, candidates=candidates, payloads=payloads)


def test_patched_planner_sandbox_is_catalogless_ordered_and_isolated(
    patched_postgres_dsn: str, tmp_path: Path
) -> None:
    import psycopg

    snapshot_path = tmp_path / "snapshot"
    write_snapshot(_snapshot(), snapshot_path)
    snapshot = load_snapshot(snapshot_path)
    universe = derive_candidate_universe(snapshot)
    materialization = materialize_native_stats(patched_postgres_dsn, snapshot, universe)
    repository_materialization = _repository_with_absent_candidate(materialization)
    repository_path = tmp_path / "native"
    write_native_stats_repository(repository_materialization, repository_path)
    repository = load_native_stats_repository(repository_path)

    prepared = prepare_postgres_planner_sandbox(
        patched_postgres_dsn, snapshot, universe, repository
    )
    assert prepared.metadata.sandbox_contract == "postgresql-planner-sandbox-v1"
    assert prepared.metadata.relation_name == snapshot.schemas[0].relation_name
    assert prepared.metadata.sample_row_count == 24
    assert prepared.metadata.population_row_count == 1000
    assert prepared.metadata.repository_candidate_count == 6
    assert prepared.metadata.repository_present_count == 5
    assert prepared.metadata.repository_absent_count == 1

    verification = verify_postgres_planner_sandbox(
        patched_postgres_dsn, snapshot, universe, repository
    )
    assert verification["target_sample_row_count"] == 24
    assert verification["frozen_sample_row_count"] == 24
    assert verification["reltuples"] == 1000
    assert verification["ordinary_stats_fingerprint"] == repository.ordinary_stats_fingerprint
    assert verification["physical_extstats_count"] == 0
    assert verification["autovacuum_disabled"] is True

    truth_path = tmp_path / "ground-truth-v1.json"
    truth = GroundTruthSet(
        snapshot.semantic_digest,
        snapshot.workload.workload_id,
        GroundTruthSource(
            PRODUCTION_EXACT_SOURCE,
            "postgresql",
            repository.server_version,
            repository.server_version_num,
            snapshot.semantic_provenance["source_view_token"],
        ),
        (CardinalityTruth("q1", 125, PRODUCTION_EXACT_SOURCE),),
    )
    write_ground_truth_set(truth, truth_path)
    loaded_truth = load_ground_truth_set(truth_path, snapshot)
    utility = WeightedWorkloadUtility(
        snapshot.workload,
        ArtifactGroundTruthProvider(loaded_truth),
        QErrorLoss(),
    )

    profile_paths = (tmp_path / "singleton-first.json", tmp_path / "singleton-second.json")
    profiles = []
    for profile_path in profile_paths:
        with PostgresPlannerSession(
            patched_postgres_dsn, snapshot, universe, repository
        ) as profiling_session:
            profile = profile_postgres_singletons(
                profiling_session,
                snapshot,
                universe,
                repository,
                utility,
                ground_truth_semantic_digest=loaded_truth.semantic_digest
                or loaded_truth.computed_semantic_digest,
            )
        write_singleton_profile(profile, profile_path)
        validate_singleton_profile(profile_path, snapshot, universe, repository, loaded_truth)
        profiles.append(load_singleton_profile(profile_path))

    first_profile, second_profile = profiles
    assert first_profile.computed_semantic_digest == second_profile.computed_semantic_digest
    assert first_profile.present_count == 5
    assert first_profile.absent_count == 1
    assert first_profile.baseline.objective > 0
    assert first_profile.frozen_ordered_candidate_ids
    assert first_profile.runtime_metadata["baseline_configuration_count"] == 1
    assert first_profile.runtime_metadata["singleton_configuration_count"] == 5
    assert first_profile.runtime_metadata["planner_query_estimate_count"] == 6
    profile_by_id = {item.candidate_id: item for item in first_profile.candidate_profiles}

    mcv = next(
        candidate
        for candidate in repository.candidate_models
        if candidate.kind == "postgresql.mcv" and candidate.column_ordinals == (1, 2)
    )
    absent = next(
        candidate for candidate in repository.candidate_models if candidate.state == ABSENT_NATIVE
    )
    assert profile_by_id[mcv.candidate_id].singleton_objective < first_profile.baseline.objective
    assert profile_by_id[mcv.candidate_id].improvement > 0
    assert (
        profile_by_id[absent.candidate_id].singleton_objective == first_profile.baseline.objective
    )
    assert profile_by_id[absent.candidate_id].improvement == 0
    assert profile_by_id[absent.candidate_id].frozen_precedence_rank is None
    assert absent.candidate_id not in first_profile.frozen_ordered_candidate_ids
    second_present = next(
        candidate
        for candidate in repository.candidate_models
        if candidate.state == "present" and candidate.candidate_id != mcv.candidate_id
    )
    with PostgresPlannerSession(patched_postgres_dsn, snapshot, universe, repository) as session_a:
        session_a.activate(PostgresStatisticsConfiguration())
        baseline = session_a.estimate_query("q1")
        baseline_utility = utility.evaluate({"q1": baseline.estimated_rows})
        session_a.activate(PostgresStatisticsConfiguration((mcv.candidate_id,)))
        mcv_estimate = session_a.estimate_query("q1")
        mcv_utility = utility.evaluate({"q1": mcv_estimate.estimated_rows})
        assert mcv_estimate.estimated_rows > baseline.estimated_rows
        assert mcv_utility.objective < baseline_utility.objective
        session_a.activate(
            PostgresStatisticsConfiguration((mcv.candidate_id, second_present.candidate_id))
        )
        ordered_ab = session_a.active_backend_oids()
        session_a.activate(
            PostgresStatisticsConfiguration((second_present.candidate_id, mcv.candidate_id))
        )
        ordered_ba = session_a.active_backend_oids()
        assert ordered_ab == (
            session_a.registered_oids[mcv.candidate_id],
            session_a.registered_oids[second_present.candidate_id],
        )
        assert ordered_ba == (
            session_a.registered_oids[second_present.candidate_id],
            session_a.registered_oids[mcv.candidate_id],
        )
        session_a.activate(PostgresStatisticsConfiguration((mcv.candidate_id,)))
        with PostgresPlannerSession(
            patched_postgres_dsn, snapshot, universe, repository
        ) as session_b:
            assert session_b.active_backend_oids() == ()
            session_b.activate(PostgresStatisticsConfiguration((second_present.candidate_id,)))
            assert session_a.active_backend_oids() == (session_a.registered_oids[mcv.candidate_id],)
            assert session_b.active_backend_oids() == (
                session_b.registered_oids[second_present.candidate_id],
            )
        with psycopg.connect(patched_postgres_dsn, autocommit=True) as connection:
            connection.execute("INSERT INTO \"public\".\"Scratch Table\" VALUES (9, 'z', 'z')")
        assert (
            session_a.connection.execute(
                'SELECT count(*) FROM "public"."Scratch Table"'
            ).fetchone()[0]
            == 24
        )
        session_a.activate(PostgresStatisticsConfiguration((mcv.candidate_id,)))
        post_commit_estimate = session_a.estimate_query("q1")
        assert post_commit_estimate.estimated_rows == mcv_estimate.estimated_rows
        session_a.activate(PostgresStatisticsConfiguration((absent.candidate_id,)))
        absent_estimate = session_a.estimate_query("q1")
        assert absent_estimate.estimated_rows == baseline.estimated_rows

    with pytest.raises(PlannerSandboxValidationError, match="sample row count drift"):
        verify_postgres_planner_sandbox(patched_postgres_dsn, snapshot, universe, repository)

    destroyed = destroy_postgres_planner_sandbox(patched_postgres_dsn)
    assert destroyed["destroyed"] is True
    with psycopg.connect(patched_postgres_dsn, autocommit=True) as connection:
        assert (
            connection.execute("SELECT to_regclass(%s)", ('"public"."Scratch Table"',)).fetchone()[
                0
            ]
            is None
        )
        assert (
            connection.execute(
                "SELECT to_regclass(%s)", ("extstats_advisor_internal.sandbox_metadata",)
            ).fetchone()[0]
            is None
        )
