from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from extstats_advisor.dbms.base import AcquisitionRequest, SamplePolicy
from extstats_advisor.dbms.postgres import PostgresSnapshotAcquirer
from extstats_advisor.dbms.postgres.deployment import deploy_postgres_recommendation
from extstats_advisor.dbms.postgres.recommendation import build_postgres_recommendation
from extstats_advisor.optimization.plan import create_optimization_plan
from tests.unit.test_greedy_add_search import _profile
from tests.unit.test_recommendation import _search, _sources

pytestmark = pytest.mark.integration


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
    search_result = _search(
        profile,
        plan,
        {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0},
    )
    snapshot_seed, universe_seed, repository_seed = _sources(profile, plan)
    relation = replace(live_snapshot.schemas[0], relation_id="rel")
    snapshot = replace(
        live_snapshot,
        schemas=(relation,),
        populations=(replace(live_snapshot.populations[0], relation_id="rel"),),
        samples={"rel": live_snapshot.samples[live_snapshot.schemas[0].relation_id]},
        semantic_digest=profile.source_snapshot_semantic_digest,
    )
    del snapshot_seed
    candidates = tuple(
        SimpleNamespace(**{**candidate.__dict__, "column_names": ("Customer ID", "Small Value")})
        for candidate in universe_seed.candidates
    )
    universe = SimpleNamespace(
        semantic_digest=universe_seed.semantic_digest,
        source_snapshot_semantic_digest=universe_seed.source_snapshot_semantic_digest,
        candidates=candidates,
    )
    repository_candidates = tuple(
        replace(candidate, column_names=("Customer ID", "Small Value"))
        for candidate in repository_seed.candidate_models
    )
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
