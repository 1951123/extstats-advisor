from __future__ import annotations

import pytest

from extstats_advisor.dbms.postgres.planner import (
    PostgresStatisticsConfiguration,
    _contains_parameter,
)
from extstats_advisor.errors import PlannerSandboxError


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
