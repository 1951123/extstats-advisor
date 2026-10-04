import pytest

from extstats_advisor.dbms.postgres.errors import SamplingResourceLimitError
from extstats_advisor.dbms.postgres.sampling import (
    deterministic_reservoir,
    initial_percentage,
    next_percentage,
    sample_percentages,
)


def test_reservoir_is_deterministic_and_bounded() -> None:
    rows = list(range(100))
    first, seen = deterministic_reservoir(rows, 10, 7, candidate_limit=100)
    second, same_seen = deterministic_reservoir(rows, 10, 7, candidate_limit=100)
    assert first == second
    assert seen == same_seen == 100
    assert len(first) == len(set(first)) == 10


def test_retry_percentage_is_monotonic_and_keeps_only_final_reservoir() -> None:
    assert initial_percentage(10, 1_000) == 2.0
    assert next_percentage(2.0) == 4.0
    sample, successful, attempts = sample_percentages(
        3,
        1_000,
        ([1], [1], [1], [1, 2, 3, 4]),
        seed=11,
        candidate_limit=20,
    )
    assert len(sample) == 3
    assert set(sample).issubset({1, 2, 3, 4})
    assert successful == 4.8
    assert [item.percentage for item in attempts] == [0.6, 1.2, 2.4, 4.8]


def test_candidate_budget_aborts_without_truncation() -> None:
    with pytest.raises(SamplingResourceLimitError, match="resource limit"):
        deterministic_reservoir(range(11), 3, 1, candidate_limit=10)
