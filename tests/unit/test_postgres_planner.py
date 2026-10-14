from __future__ import annotations

import pytest

from extstats_advisor.dbms.postgres.planner import (
    PostgresPlannerSession,
    PostgresStatisticsConfiguration,
    _contains_parameter,
)
from extstats_advisor.errors import PlannerQueryError, PlannerSandboxError


def test_postgres_configuration_preserves_order_and_membership() -> None:
    configuration = PostgresStatisticsConfiguration(("cand_b", "cand_a"))
    configuration.validate(("cand_a", "cand_b"))
    assert configuration.ordered_candidate_ids == ("cand_b", "cand_a")
    assert configuration.membership == frozenset({"cand_a", "cand_b"})


def test_postgres_configuration_accepts_empty_baseline_and_absent_candidate() -> None:
    PostgresStatisticsConfiguration().validate(("cand_absent",))
    configuration = PostgresStatisticsConfiguration(("cand_absent",))
    configuration.validate(("cand_absent",))


def test_postgres_configuration_rejects_duplicates_and_unknown_ids() -> None:
    with pytest.raises(PlannerSandboxError, match="duplicate"):
        PostgresStatisticsConfiguration(("cand_a", "cand_a")).validate(("cand_a",))
    with pytest.raises(PlannerSandboxError, match="unknown"):
        PostgresStatisticsConfiguration(("cand_missing",)).validate(("cand_a",))


def test_parameterized_sql_is_detected_for_fail_closed_planning() -> None:
    assert _contains_parameter("SELECT 1") is False
    assert _contains_parameter("SELECT * FROM t WHERE a = $1") is True


def _planner_with_node(node):
    session = object.__new__(PostgresPlannerSession)
    session._plan_node = lambda _query_id: node
    return session


def test_ordinary_plan_rows_contract_remains_integer_valued() -> None:
    session = _planner_with_node({"Plan Rows": 1234, "Plan Rows Exact": 1234.125})

    estimate = session.estimate_query("q1")

    assert estimate.query_id == "q1"
    assert estimate.estimated_rows == 1234
    assert isinstance(estimate.estimated_rows, int)


def test_precise_plan_rows_preserves_fractional_diagnostic_value() -> None:
    session = _planner_with_node({"Plan Rows": 1234, "Plan Rows Exact": 1234.125})

    estimate = session.estimate_query_precise("q1")

    assert estimate.query_id == "q1"
    assert estimate.estimated_rows == 1234.125
    assert estimate.formatted_estimated_rows == 1234


def test_precise_plan_rows_fails_without_diagnostic_field() -> None:
    session = _planner_with_node({"Plan Rows": 1234})

    with pytest.raises(PlannerQueryError, match="Plan Rows Exact"):
        session.estimate_query_precise("q1")
