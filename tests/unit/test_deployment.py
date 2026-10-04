from __future__ import annotations

from dataclasses import replace
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
from extstats_advisor.deployment.model import DeployedObject, DeploymentResult
from extstats_advisor.errors import (
    DeploymentCommitOutcomeUnknownError,
    DeploymentCommittedButUnverifiedError,
    DeploymentMutationError,
    DeploymentValidationError,
)
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


def test_no_change_deployment_is_deterministic_and_does_not_connect(monkeypatch) -> None:
    sources = _source_chain(no_change=True)
    monkeypatch.setattr(
        deployment_module, "_connect", lambda *args, **kwargs: pytest.fail("no-change connected")
    )
    result = deploy_postgres_recommendation(None, *sources)

    assert result.decision == DECISION_NO_CHANGE
    assert result.commit_status == "not-required"
    assert result.target_relation_oid is None
    assert result.deployed_objects == ()
    assert result.deployment_ordered_candidate_ids == ()
    assert result.post_commit_verified is True
    assert result.target_schema == sources[-1].target_relation.schema
    assert result.target_relation == sources[-1].target_relation.name
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


def _successful_result():
    sources = _source_chain()
    recommendation = sources[-1]
    objects = tuple(
        DeployedObject(
            candidate.candidate_id,
            candidate.statistics_object.schema,
            candidate.statistics_object.name,
            oid,
            candidate.kind,
            candidate.column_ordinals,
            candidate.statistics_target,
            True,
            candidate.deployment_order_position,
        )
        for candidate, oid in zip(recommendation.selected_candidates, (101, 203, 207), strict=True)
    )
    result = DeploymentResult(
        recommendation.computed_semantic_digest,
        recommendation.source_snapshot_semantic_digest,
        recommendation.candidate_universe_semantic_digest,
        recommendation.native_stats_repository_semantic_digest,
        recommendation.singleton_profile_semantic_digest,
        recommendation.optimization_plan_semantic_digest,
        recommendation.search_result_semantic_digest,
        recommendation.deployment_contract,
        "postgresql-add-only-deployment-v1",
        recommendation.target_relation.catalog,
        recommendation.target_relation.schema,
        recommendation.target_relation.name,
        42,
        recommendation.dbms_source_version,
        sources[2].server_version_num,
        recommendation.decision,
        recommendation.deployment_ordered_candidate_ids,
        objects,
        {"status": "ready", "relation_oid": 42},
        "committed",
        True,
        {"lock_timeout_ms": 5_000, "statement_timeout_ms": 120_000},
    )
    return sources, recommendation, result


@pytest.mark.parametrize("tamper", ("name", "kind", "columns", "statistics_target", "target"))
def test_source_aware_validation_rejects_tampered_deployed_mapping(
    tmp_path: Path, tamper: str
) -> None:
    sources, recommendation, result = _successful_result()
    object_value = result.deployed_objects[0]
    changes = {
        "name": {"name": "wrong_deterministic_name"},
        "kind": {"kind": "postgresql.dependencies"},
        "columns": {"column_ordinals": (1, 3)},
        "statistics_target": {"statistics_target": 101},
        "target": {},
    }
    if tamper == "target":
        tampered_result = replace(result, target_schema="wrong_schema")
    else:
        tampered_result = replace(
            result,
            deployed_objects=(
                replace(object_value, **changes[tamper]),
                *result.deployed_objects[1:],
            ),
            semantic_digest=None,
        )
    path = tmp_path / f"tampered-{tamper}.json"
    write_deployment_result(tampered_result, path)
    with pytest.raises(DeploymentValidationError):
        validate_deployment_result(
            path,
            recommendation,
            source_snapshot=sources[0],
            candidate_universe=sources[1],
            native_repository=sources[2],
            singleton_profile=sources[4],
            optimization_plan=sources[5],
            search_result=sources[6],
        )


@pytest.mark.parametrize("tamper", ("duplicate_oid", "wrong_order", "wrong_server_version_num"))
def test_source_aware_validation_rejects_tampered_physical_evidence(
    tmp_path: Path, tamper: str
) -> None:
    sources, recommendation, result = _successful_result()
    if tamper == "duplicate_oid":
        objects = (
            result.deployed_objects[0],
            replace(result.deployed_objects[1], oid=result.deployed_objects[0].oid),
            result.deployed_objects[2],
        )
        with pytest.raises(DeploymentValidationError):
            replace(result, deployed_objects=objects, semantic_digest=None)
        return
    elif tamper == "wrong_order":
        with pytest.raises(DeploymentValidationError):
            replace(
                result,
                deployment_ordered_candidate_ids=("B", "A", "C"),
                semantic_digest=None,
            )
        return
    else:
        tampered_result = replace(result, server_version_num=160015, semantic_digest=None)
    path = tmp_path / f"tampered-{tamper}.json"
    write_deployment_result(tampered_result, path)
    with pytest.raises(DeploymentValidationError):
        validate_deployment_result(
            path,
            recommendation,
            source_snapshot=sources[0],
            candidate_universe=sources[1],
            native_repository=sources[2],
            singleton_profile=sources[4],
            optimization_plan=sources[5],
            search_result=sources[6],
        )


def test_source_aware_validation_rejects_tampered_target_and_server_identity(
    tmp_path: Path,
) -> None:
    sources, recommendation, result = _successful_result()
    for label, tampered in (
        ("target", replace(result, target_relation="wrong_relation")),
        ("server", replace(result, server_version="16.15")),
    ):
        path = tmp_path / f"tampered-{label}.json"
        write_deployment_result(tampered, path)
        with pytest.raises(DeploymentValidationError):
            validate_deployment_result(
                path,
                recommendation,
                source_snapshot=sources[0],
                candidate_universe=sources[1],
                native_repository=sources[2],
                singleton_profile=sources[4],
                optimization_plan=sources[5],
                search_result=sources[6],
            )


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
    def __init__(self, commit_error: Exception | None = None) -> None:
        self.calls: list[object] = []
        self.commit_error = commit_error

    def execute(self, query, params=None):
        self.calls.append((query, params))

    def commit(self) -> None:
        self.calls.append("COMMIT")
        if self.commit_error is not None:
            raise self.commit_error

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


def test_commit_error_has_unknown_outcome_and_does_not_verify_postcommit(monkeypatch) -> None:
    sources = _source_chain()
    connection = _TransactionConnection(RuntimeError("lost commit confirmation"))

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
    monkeypatch.setattr(deployment_module, "_execute_actions", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployment_module, "_execute_analyze", lambda *args, **kwargs: None)
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
    monkeypatch.setattr(deployment_module, "_read_managed_objects", lambda *args, **kwargs: objects)
    postverify_called = False

    def fail_if_postverified(*args, **kwargs):
        nonlocal postverify_called
        postverify_called = True
        raise AssertionError("post-commit verification must not run")

    monkeypatch.setattr(deployment_module, "_post_commit_verify", fail_if_postverified)

    with pytest.raises(DeploymentCommitOutcomeUnknownError, match="outcome is unknown"):
        deploy_postgres_recommendation("postgresql://db", *sources)

    assert postverify_called is False
    assert connection.calls.count("COMMIT") == 1
    assert connection.calls.count("ROLLBACK") == 0
    assert connection.calls.count("CLOSE") == 1


def test_confirmed_commit_postverify_failure_is_committed_but_unverified(monkeypatch) -> None:
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
    monkeypatch.setattr(deployment_module, "_execute_actions", lambda *args, **kwargs: None)
    monkeypatch.setattr(deployment_module, "_execute_analyze", lambda *args, **kwargs: None)
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
    monkeypatch.setattr(deployment_module, "_read_managed_objects", lambda *args, **kwargs: objects)
    monkeypatch.setattr(
        deployment_module,
        "_post_commit_verify",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("verification failed")),
    )

    with pytest.raises(DeploymentCommittedButUnverifiedError):
        deploy_postgres_recommendation("postgresql://db", *sources)

    assert connection.calls.count("COMMIT") == 1
    assert connection.calls.count("ROLLBACK") == 0
