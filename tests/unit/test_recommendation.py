from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from extstats_advisor.dbms.postgres.recommendation import (
    build_postgres_recommendation,
    postgres_statistics_object_name,
    quote_postgresql_identifier,
    render_postgres_sql,
)
from extstats_advisor.errors import RecommendationValidationError, SearchBudgetExpired
from extstats_advisor.native_stats.model import PRESENT, NativeStatsCandidate
from extstats_advisor.optimization.plan import create_optimization_plan
from extstats_advisor.optimization.search import greedy_add_search
from extstats_advisor.recommendation.artifact import (
    load_recommendation,
    validate_recommendation,
    write_recommendation,
)
from extstats_advisor.recommendation.model import DECISION_NO_CHANGE, DECISION_PROPOSE_CHANGE
from extstats_advisor.snapshot.model import ColumnSchema, DBMSIdentity, RelationName, RelationSchema
from tests.unit.test_greedy_add_search import (
    _identity,
    _profile,
    _Utility,
    _utility_result,
)


def _sources(profile, plan):
    snapshot_digest = profile.source_snapshot_semantic_digest
    universe_digest = profile.candidate_universe_semantic_digest
    repository_digest = profile.native_stats_repository_semantic_digest
    relation = RelationSchema(
        "rel",
        RelationName('Order "Facts', schema="Reporting.Schema", catalog="warehouse"),
        (
            ColumnSchema("Customer ID", 1, "string", True, "text"),
            ColumnSchema("城市", 2, "string", True, "text"),
        ),
    )
    snapshot = SimpleNamespace(
        semantic_digest=snapshot_digest,
        schemas=(relation,),
        schema_by_id={relation.relation_id: relation},
        dbms=DBMSIdentity("postgresql", "16.14"),
    )
    candidate_models = tuple(
        SimpleNamespace(
            candidate_id=candidate_id,
            relation_id="rel",
            kind=kind,
            column_ordinals=(1, 2),
            column_names=("Customer ID", "城市"),
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
    universe = SimpleNamespace(
        semantic_digest=universe_digest,
        source_snapshot_semantic_digest=snapshot_digest,
        candidates=candidate_models,
    )
    repository = SimpleNamespace(
        semantic_digest=repository_digest,
        source_snapshot_semantic_digest=snapshot_digest,
        candidate_universe_semantic_digest=universe_digest,
        candidate_models=candidates,
        statistics_target=100,
        server_version="16.14",
    )
    return snapshot, universe, repository


def _search(profile, plan, objectives, *, budget_expired=False):
    def evaluate(membership, _deadline):
        ordered = plan.ordered_configuration(membership)
        if budget_expired:
            raise SearchBudgetExpired("test budget")
        return _utility_result(objectives[ordered])

    return greedy_add_search(profile, plan, _Utility(), evaluate, _identity())


def test_recommendation_uses_singleton_order_not_accepted_move_order(tmp_path: Path) -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)
    search_result = _search(
        profile,
        plan,
        {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0},
    )
    snapshot, universe, repository = _sources(profile, plan)
    recommendation = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, search_result
    )

    assert [move.added_candidate_id for move in search_result.accepted_moves] == ["A", "C", "B"]
    assert recommendation.decision == DECISION_PROPOSE_CHANGE
    assert recommendation.selected_candidate_ids == ("A", "B", "C")
    assert recommendation.deployment_ordered_candidate_ids == ("A", "B", "C")
    assert [item.deployment_order_position for item in recommendation.selected_candidates] == [
        1,
        2,
        3,
    ]
    assert [action.action_type for action in recommendation.ddl_plan] == [
        "create-statistics",
        "set-statistics-target",
        "create-statistics",
        "set-statistics-target",
        "create-statistics",
        "set-statistics-target",
        "analyze-relation",
    ]
    assert all(item.statistics_target == 100 for item in recommendation.selected_candidates)
    assert len({item.statistics_object.name for item in recommendation.selected_candidates}) == 3

    path = tmp_path / "recommendation-v1.json"
    write_recommendation(recommendation, path)
    assert (
        load_recommendation(path).computed_semantic_digest
        == recommendation.computed_semantic_digest
    )
    validate_recommendation(
        path, snapshot, universe, repository, None, profile, plan, search_result
    )
    assert (
        recommendation.computed_semantic_digest
        == build_postgres_recommendation(
            snapshot, universe, repository, profile, plan, search_result
        ).computed_semantic_digest
    )


def test_recommendation_sql_quotes_identifiers_and_analyzes_once() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)
    search_result = _search(
        profile,
        plan,
        {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0},
    )
    snapshot, universe, repository = _sources(profile, plan)
    recommendation = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, search_result
    )
    sql = render_postgres_sql(recommendation)
    assert 'CREATE STATISTICS "Reporting.Schema".' in sql
    assert 'ON "Customer ID", "城市"' in sql
    assert 'FROM "Reporting.Schema"."Order ""Facts";' in sql
    assert sql.count("ANALYZE ") == 1
    assert "IF NOT EXISTS" not in sql
    assert quote_postgresql_identifier('a"b') == '"a""b"'


def test_recommendation_no_change_and_budget_expiry_preserve_search_state() -> None:
    profile = _profile((10.0, 10.0, 12.0))
    plan = create_optimization_plan(profile, candidate_limit=3)
    no_change_result = _search(profile, plan, {}, budget_expired=False)
    snapshot, universe, repository = _sources(profile, plan)
    no_change = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, no_change_result
    )
    assert no_change.decision == DECISION_NO_CHANGE
    assert no_change.ddl_plan == ()
    assert render_postgres_sql(no_change) == ""

    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3, wall_clock_seconds=1.0)
    budget_result = _search(profile, plan, {}, budget_expired=True)
    snapshot, universe, repository = _sources(profile, plan)
    budget_recommendation = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, budget_result
    )
    assert budget_result.termination_reason == "budget-expired-incomplete-round"
    assert budget_recommendation.search_termination_reason == budget_result.termination_reason
    assert budget_recommendation.decision == DECISION_PROPOSE_CHANGE
    assert budget_recommendation.selected_candidate_ids == ("A",)


def test_recommendation_rejects_tampered_search_order_and_metadata() -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)
    search_result = _search(
        profile,
        plan,
        {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0},
    )
    snapshot, universe, repository = _sources(profile, plan)
    tampered = replace(
        search_result,
        final_ordered_candidate_ids=("A", "C", "B"),
        semantic_digest=None,
    )
    with pytest.raises(RecommendationValidationError):
        build_postgres_recommendation(snapshot, universe, repository, profile, plan, tampered)

    repository_values = dict(repository.__dict__)
    repository_values["candidate_models"] = tuple(
        replace(candidate, column_names=("wrong", "城市"))
        for candidate in repository.candidate_models
    )
    bad_repository = SimpleNamespace(**repository_values)
    with pytest.raises(RecommendationValidationError):
        build_postgres_recommendation(
            snapshot, universe, bad_repository, profile, plan, search_result
        )


def test_statistics_names_are_deterministic_and_bounded() -> None:
    mcv_name = postgres_statistics_object_name("cand_a", "postgresql.mcv")
    dependency_name = postgres_statistics_object_name("cand_a", "postgresql.dependencies")
    assert mcv_name == postgres_statistics_object_name("cand_a", "postgresql.mcv")
    assert mcv_name != dependency_name
    assert len(mcv_name.encode()) <= 63
    assert len(dependency_name.encode()) <= 63
    assert mcv_name.startswith("extstats_adv_mcv_")
    assert dependency_name.startswith("extstats_adv_dep_")


def test_recommendation_artifact_rejects_tampered_action(tmp_path: Path) -> None:
    profile = _profile()
    plan = create_optimization_plan(profile, candidate_limit=3)
    search_result = _search(
        profile,
        plan,
        {("A", "B"): 6.0, ("A", "C"): 4.0, ("A", "B", "C"): 3.0},
    )
    snapshot, universe, repository = _sources(profile, plan)
    recommendation = build_postgres_recommendation(
        snapshot, universe, repository, profile, plan, search_result
    )
    path = tmp_path / "recommendation.json"
    write_recommendation(recommendation, path)
    value = json.loads(path.read_text())
    value["ddl_plan"][0]["object_name"] = "extstats_adv_mcv_tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(RecommendationValidationError):
        load_recommendation(path)
