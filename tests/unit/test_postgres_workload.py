from __future__ import annotations

import pytest

from extstats_advisor.candidates.groups import derive_relevant_groups
from extstats_advisor.dbms.postgres.workload import analyze_query
from extstats_advisor.errors import CandidateGenerationError
from extstats_advisor.snapshot.model import (
    ColumnSchema,
    RelationName,
    RelationSchema,
    WorkloadQuery,
)


@pytest.fixture
def order_schema() -> RelationSchema:
    names = ("Customer ID", "城市", "Amount", "Unused")
    return RelationSchema(
        "rel_order_facts",
        RelationName("Order Facts", schema="Reporting.Schema", catalog="postgres"),
        tuple(ColumnSchema(name, index, "text", True) for index, name in enumerate(names, 1)),
    )


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "Reporting.Schema"."Order Facts" AS o WHERE o."Customer ID" = $1 AND "城市" <> $2',
        'SELECT * FROM "Order Facts" WHERE "Customer ID" < 10 AND 1 <= "城市"',
        'SELECT * FROM "Order Facts" WHERE "Customer ID" BETWEEN 1 AND 9 AND "城市" IN ($1, $2)',
        'SELECT * FROM "Order Facts" WHERE "Customer ID" IS NULL AND "城市" IS NOT NULL',
    ],
)
def test_supported_predicate_profiles_preserve_native_names(order_schema, sql) -> None:
    profile = analyze_query(WorkloadQuery("q1", sql), order_schema)
    assert profile.analysis_status == "supported"
    assert profile.relation_id == order_schema.relation_id
    assert profile.predicate_column_ordinals == (1, 2)
    assert profile.predicate_column_names == ("Customer ID", "城市")


@pytest.mark.parametrize(
    "sql",
    [
        'SELECT * FROM "Order Facts" a JOIN other b ON a."Customer ID" = b.id',
        'SELECT * FROM "Order Facts" WHERE "Customer ID" = 1 OR "城市" = 2',
        'SELECT * FROM "Order Facts" WHERE NOT ("Customer ID" = 1)',
        'SELECT * FROM "Order Facts" WHERE "Customer ID" IN (SELECT id FROM other)',
        'WITH x AS (SELECT * FROM "Order Facts") SELECT * FROM x',
        'SELECT * FROM "Order Facts" WHERE "Customer ID" = "城市"',
        'SELECT * FROM "Order Facts" WHERE lower("城市") = $1',
        'SELECT * FROM "Wrong Facts" WHERE "Customer ID" = $1',
        'SELECT * FROM "Order Facts" WHERE "Unknown" = $1',
    ],
)
def test_out_of_scope_queries_are_classified_without_guessing(order_schema, sql) -> None:
    profile = analyze_query(WorkloadQuery("q_bad", sql), order_schema)
    assert profile.analysis_status == "unsupported"
    assert profile.query_id == "q_bad"
    assert profile.reason


def test_positive_unsupported_query_fails_closed(order_schema) -> None:
    profile = analyze_query(
        WorkloadQuery("q_bad", 'SELECT * FROM "Order Facts" WHERE "Customer ID" = "城市"'),
        order_schema,
    )
    with pytest.raises(CandidateGenerationError, match="q_bad"):
        derive_relevant_groups(order_schema, (profile,))


def test_zero_weight_unsupported_query_does_not_create_groups(order_schema) -> None:
    profile = analyze_query(
        WorkloadQuery("q_zero", 'SELECT * FROM "Order Facts" WHERE "Customer ID" = "城市"', 0.0),
        order_schema,
    )
    assert derive_relevant_groups(order_schema, (profile,)) == ()
