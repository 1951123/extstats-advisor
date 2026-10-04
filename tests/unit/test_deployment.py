from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import extstats_advisor.dbms.postgres.deployment as deployment_module
from extstats_advisor.dbms.postgres.deployment import (
    _name_collisions,
    _read_managed_objects,
    deploy_postgres_recommendation,
)
from extstats_advisor.dbms.postgres.recommendation import build_postgres_recommendation
from extstats_advisor.deployment.artifact import (
    load_deployment_result,
    validate_deployment_result,
    write_deployment_result,
)
from extstats_advisor.deployment.model import DeployedObject
from extstats_advisor.errors import DeploymentMutationError, DeploymentValidationError
from extstats_advisor.optimization.plan import create_optimization_plan
from extstats_advisor.recommendation.model import DECISION_NO_CHANGE
from tests.unit.test_greedy_add_search import _profile
from tests.unit.test_recommendation import _search, _sources


def _source_chain(*, no_change: bool = False):
    profile = _profile((10.0, 10.0, 12.0)) if no_change else _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)
    objectives = (
        {}
        if no_change
        else {
            ("A", "B"): 6.0,
            ("A", "C"): 4.0,
            ("A", "B", "C"): 3.0,
        }
    )
    search_result = _search(profile, plan, objectives)
    snapshot, universe, repository = _sources(profile, plan)
    repository = SimpleNamespace(**repository.__dict__, server_version_num=160014)
    recommendation = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, search_result
    )
    ground_truth = SimpleNamespace(semantic_digest=profile.ground_truth_semantic_digest)
    return (
        snapshot,
        universe,
        repository,
        ground_truth,
        profile,
        plan,
        search_result,
        recommendation,
    )


def test_no_change_deployment_is_deterministic_and_does_not_connect() -> None:
    sources = _source_chain(no_change=True)
    result = deploy_postgres_recommendation(None, *sources)

    assert result.decision == DECISION_NO_CHANGE
    assert result.commit_status == "not-required"
    assert result.target_relation_oid is None
    assert result.preflight_summary["status"] == "not-run"
    assert result.computed_semantic_digest == result.computed_semantic_digest


def test_deployment_result_round_trip_and_source_validation(tmp_path: Path) -> None:
    sources = _source_chain(no_change=True)
    result = deploy_postgres_recommendation(None, *sources)
    path = tmp_path / "deployment-result.json"

    digest = write_deployment_result(result, path)
    loaded = load_deployment_result(path)
    summary = validate_deployment_result(
        path,
        sources[-1],
        source_snapshot=sources[0],
        candidate_universe=sources[1],
        native_repository=sources[2],
        singleton_profile=sources[4],
        optimization_plan=sources[5],
        search_result=sources[6],
    )

    assert digest == loaded.computed_semantic_digest
    assert summary["decision"] == "no-change"
    assert "dsn" not in path.read_text()


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0] if self.rows else None


class _CatalogConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, query, params):
        if "stxname = ANY" in query:
            return _Rows(self.rows)
        return _Rows([("1",)])


def test_managed_verification_allows_external_oid_interleaving() -> None:
    sources = _source_chain()
    recommendation = sources[-1]
    rows = [
        (
            101,
            42,
            "Reporting.Schema",
            recommendation.selected_candidates[0].statistics_object.name,
            "{m}",
            "1 2",
            100,
            True,
            False,
        ),
        (
            203,
            42,
            "Reporting.Schema",
            recommendation.selected_candidates[1].statistics_object.name,
            "{f}",
            "1 2",
            100,
            False,
            True,
        ),
        (
            207,
            42,
            "Reporting.Schema",
            recommendation.selected_candidates[2].statistics_object.name,
            "{m}",
            "1 2",
            100,
            True,
            False,
        ),
    ]

    observed = _read_managed_objects(
        _CatalogConnection(rows), recommendation, 42, require_payload=True
    )

    assert [item.oid for item in observed] == [101, 203, 207]
    assert [item.candidate_id for item in observed] == list(
        recommendation.deployment_ordered_candidate_ids
    )


def test_exact_name_collision_is_fail_closed() -> None:
    sources = _source_chain()
    recommendation = sources[-1]
    collisions = _name_collisions(_CatalogConnection([]), recommendation)
    assert collisions == [
        f"{item.statistics_object.schema}.{item.statistics_object.name}"
        for item in recommendation.selected_candidates
    ]


def test_deployed_object_rejects_unverified_payload() -> None:
    with pytest.raises(DeploymentValidationError):
        DeployedObject("A", "public", "stats_a", 0, "postgresql.mcv", (1, 2), 100, True, 1)


class _TransactionConnection:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def commit(self) -> None:
        self.calls.append("COMMIT")

    def rollback(self) -> None:
        self.calls.append("ROLLBACK")

    def close(self) -> None:
        self.calls.append("CLOSE")


def test_deployment_uses_one_explicit_transaction_and_commits_after_verification(
    monkeypatch,
) -> None:
    sources = _source_chain()
    connection = _TransactionConnection()
    objects = tuple(
        DeployedObject(
            item.candidate_id,
            item.statistics_object.schema,
            item.statistics_object.name,
            index,
            item.kind,
            item.column_ordinals,
            item.statistics_target,
            True,
            item.deployment_order_position,
        )
        for index, item in enumerate(sources[-1].selected_candidates, start=101)
    )

    class _Psycopg:
        Error = RuntimeError
        sql = object()

        @staticmethod
        def connect(*args, **kwargs):
            _Psycopg.connect_args = (args, kwargs)
            return connection

    monkeypatch.setattr(deployment_module, "_psycopg", lambda: _Psycopg)
    monkeypatch.setattr(deployment_module, "_validate_source_chain", lambda *args: sources[-1])
    monkeypatch.setattr(
        deployment_module,
        "_preflight_locked",
        lambda *args, **kwargs: {
            "relation_oid": 42,
            "server_version": "16.14",
            "server_version_num": 160014,
        },
    )
    monkeypatch.setattr(deployment_module, "_execute_actions", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployment_module, "_execute_analyze", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployment_module, "_read_managed_objects", lambda *args, **kwargs: objects)
    monkeypatch.setattr(deployment_module, "_post_commit_verify", lambda *args, **kwargs: None)

    result = deploy_postgres_recommendation("postgresql://db", *sources)

    assert result.commit_status == "committed"
    assert _Psycopg.connect_args[1]["autocommit"] is True
    assert next(call for call in connection.calls if isinstance(call, tuple))[0] == "BEGIN"
    assert connection.calls.count("COMMIT") == 1
    assert connection.calls.count("ROLLBACK") == 0
    assert connection.calls.count("CLOSE") == 1


def test_deployment_rolls_back_and_closes_on_precommit_failure(monkeypatch) -> None:
    sources = _source_chain()
    connection = _TransactionConnection()

    class _Psycopg:
        Error = RuntimeError
        sql = object()

        @staticmethod
        def connect(*args, **kwargs):
            return connection

    monkeypatch.setattr(deployment_module, "_psycopg", lambda: _Psycopg)
    monkeypatch.setattr(deployment_module, "_validate_source_chain", lambda *args: sources[-1])
    monkeypatch.setattr(
        deployment_module,
        "_preflight_locked",
        lambda *args, **kwargs: {
            "relation_oid": 42,
            "server_version": "16.14",
            "server_version_num": 160014,
        },
    )
    monkeypatch.setattr(
        deployment_module,
        "_execute_actions",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with pytest.raises(DeploymentMutationError):
        deploy_postgres_recommendation("postgresql://db", *sources)

    assert connection.calls.count("ROLLBACK") == 1
    assert connection.calls.count("CLOSE") == 1
