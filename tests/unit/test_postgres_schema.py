from extstats_advisor.dbms.postgres.schema import opaque_relation_id
from extstats_advisor.snapshot.model import RelationName


def test_relation_id_is_opaque_and_structured_name_sensitive() -> None:
    first = opaque_relation_id(RelationName("Order Facts", "Reporting.Schema", "db"))
    second = opaque_relation_id(RelationName("Order Facts", "Reporting.Schema", "other-db"))
    assert first.startswith("rel_")
    assert len(first) == 28
    assert first != second
    assert "Order" not in first
