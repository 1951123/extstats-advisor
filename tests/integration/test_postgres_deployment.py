from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from extstats_advisor.dbms.base import AcquisitionRequest, SamplePolicy
from extstats_advisor.dbms.postgres import PostgresSnapshotAcquirer
from extstats_advisor.dbms.postgres.deployment import deploy_postgres_recommendation
from extstats_advisor.dbms.postgres.recommendation import build_postgres_recommendation
from extstats_advisor.native_stats.model import PRESENT, NativeStatsCandidate
from extstats_advisor.optimization.plan import create_optimization_plan
from extstats_advisor.optimization.search import PlannerIdentity, greedy_add_search
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
)
from extstats_advisor.snapshot.model import ColumnSchema, DBMSIdentity, RelationName, RelationSchema
from extstats_advisor.utility.model import PerQueryUtility, UtilityResult

pytestmark = pytest.mark.integration


class _Utility:
    utility_contract = "weighted-workload-mean-v1"
    loss_contract = "qerror-cardinality-floor-1-v1"

    def evaluate(self, estimates):
        objective = float(next(iter(estimates.values())))
        return UtilityResult(
            objective,
            self.loss_contract,
            1,
            1.0,
            (PerQueryUtility("q1", 1.0, objective, 1, objective),),
        )


def _profile() -> SingletonProfile:
    candidates = tuple(
        CandidateSingletonProfile(candidate_id, PRESENT, index, objective, 10.0 - objective, index)
        for index, (candidate_id, objective) in enumerate(
            zip(("A", "B", "C"), (7.0, 8.0, 12.0)), start=1
        )
    )
    return SingletonProfile(
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "d" * 64,
        "sandbox-v1",
        "backend-v1",
        "16.14",
        160014,
        "e" * 64,
        "weighted-workload-mean-v1",
        "qerror-cardinality-floor-1-v1",
        SINGLETON_PRECEDENCE_POLICY,
        BaselineProfile(10.0),
        candidates,
        ("A", "B", "C"),
    )


def _search(profile, plan):
    objectives = {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0}

    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        objective = objectives[ordered]
        return UtilityResult(
            objective,
            "qerror-cardinality-floor-1-v1",
            1,
            1.0,
            (PerQueryUtility("q1", 1.0, objective, 1, objective),),
        )

    return greedy_add_search(
        profile,
        plan,
        _Utility(),
        evaluate,
        PlannerIdentity("sandbox-v1", "backend-v1", "16.14", 160014, "e" * 64),
    )


def _source_seeds(profile):
    relation = RelationSchema(
        "rel",
        RelationName("Order Facts", schema="Reporting.Schema", catalog="postgres"),
        (
            ColumnSchema("Customer ID", 1, "int64", False, "bigint"),
            ColumnSchema("Small Value", 2, "int16", True, "smallint"),
        ),
    )
    candidate_models = tuple(
        SimpleNamespace(
            candidate_id=candidate_id,
            relation_id="rel",
            kind=kind,
            column_ordinals=(1, 2),
            column_names=("Customer ID", "Small Value"),
        )
        for candidate_id, kind in (
            ("A", "postgresql.mcv"),
            ("B", "postgresql.dependencies"),
            ("C", "postgresql.mcv"),
        )
    )
    candidates = tuple(
        NativeStatsCandidate(
            candidate.candidate_id,
            candidate.relation_id,
            candidate.kind,
            candidate.column_ordinals,
            candidate.column_names,
            PRESENT,
            "json",
            1,
            "e" * 64,
            f"payloads/{candidate.candidate_id}.bin",
        )
        for candidate in candidate_models
    )
    snapshot = SimpleNamespace(
        semantic_digest=profile.source_snapshot_semantic_digest,
        schemas=(relation,),
        schema_by_id={"rel": relation},
        dbms=DBMSIdentity("postgresql", "16.14"),
    )
    universe = SimpleNamespace(
        semantic_digest=profile.candidate_universe_semantic_digest,
        source_snapshot_semantic_digest=profile.source_snapshot_semantic_digest,
        candidates=candidate_models,
    )
    repository = SimpleNamespace(
        semantic_digest=profile.native_stats_repository_semantic_digest,
        source_snapshot_semantic_digest=profile.source_snapshot_semantic_digest,
        candidate_universe_semantic_digest=profile.candidate_universe_semantic_digest,
        candidate_models=candidates,
        statistics_target=100,
        server_version="16.14",
    )
    return snapshot, universe, repository


def test_stock_postgres_deployment_is_transactional_and_add_only(
    postgres_capture_dsn: str, postgres_admin_dsn: str
) -> None:
    import psycopg

    with psycopg.connect(postgres_admin_dsn, autocommit=True) as connection:
        connection.execute(
            'CREATE STATISTICS "Reporting.Schema"."external_before" (mcv) '
            'ON "Customer ID", "Small Value" FROM "Reporting.Schema"."Order Facts"'
        )
        connection.execute('ANALYZE "Reporting.Schema"."Order Facts"')

    live_snapshot = PostgresSnapshotAcquirer(postgres_capture_dsn).capture(
        AcquisitionRequest(
            '"Reporting.Schema"."Order Facts"',
            SamplePolicy(8, seed=17),
        ),
    )
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)
    search_result = _search(profile, plan)
    _, universe_seed, repository_seed = _source_seeds(profile)
    relation = replace(live_snapshot.schemas[0], relation_id="rel")
    snapshot = replace(
        live_snapshot,
        schemas=(relation,),
        populations=(replace(live_snapshot.populations[0], relation_id="rel"),),
        samples={"rel": live_snapshot.samples[live_snapshot.schemas[0].relation_id]},
        semantic_digest=profile.source_snapshot_semantic_digest,
    )
    universe = universe_seed
    repository_candidates = repository_seed.candidate_models
    with psycopg.connect(postgres_admin_dsn, autocommit=True) as connection:
        server_version = str(connection.execute("SHOW server_version").fetchone()[0])
        server_version_num = int(connection.execute("SHOW server_version_num").fetchone()[0])
    repository_values = dict(repository_seed.__dict__)
    repository_values.update(
        candidate_models=repository_candidates,
        server_version=server_version,
        server_version_num=server_version_num,
    )
    repository = SimpleNamespace(**repository_values)
    ground_truth = SimpleNamespace(semantic_digest=profile.ground_truth_semantic_digest)
    recommendation = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, search_result
    )

    result = deploy_postgres_recommendation(
        postgres_admin_dsn,
        snapshot,
        universe,
        repository,
        ground_truth,
        profile,
        plan,
        search_result,
        recommendation,
        lock_timeout_ms=5_000,
        statement_timeout_ms=30_000,
    )

    assert result.commit_status == "committed"
    assert result.post_commit_verified is True
    assert result.preflight_summary["existing_external_statistics_names"] == [
        "Reporting.Schema.external_before"
    ]
    assert result.preflight_summary["server_version_num"] == server_version_num
    assert [item.candidate_id for item in result.deployed_objects] == ["A", "B", "C"]
    assert (
        result.deployed_objects[0].oid
        < result.deployed_objects[1].oid
        < result.deployed_objects[2].oid
    )

    with psycopg.connect(postgres_admin_dsn, autocommit=True) as connection:
        rows = connection.execute(
            "SELECT e.stxname FROM pg_catalog.pg_statistic_ext AS e "
            "JOIN pg_catalog.pg_namespace AS n ON n.oid = e.stxnamespace "
            "WHERE n.nspname = %s ORDER BY e.oid",
            ("Reporting.Schema",),
        ).fetchall()
    names = {str(row[0]) for row in rows}
    assert "external_before" in names
    assert {item.name for item in result.deployed_objects} <= names
