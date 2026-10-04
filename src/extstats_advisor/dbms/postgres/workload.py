"""PostgreSQL-specific SQL parsing into portable predicate profiles."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from extstats_advisor.errors import CandidateGenerationError
from extstats_advisor.snapshot.model import RelationSchema, Workload, WorkloadQuery
from extstats_advisor.workload.analysis import (
    ANALYSIS_CONTRACT_VERSION,
    SUPPORTED_ANALYSIS_STATUS,
    UNSUPPORTED_ANALYSIS_STATUS,
    PredicateProfile,
)

_SUPPORTED_OPERATORS = {"=", "<>", "<", "<=", ">", ">="}


def _pglast() -> tuple[Any, Any, Any, Any, Any]:
    try:
        import pglast
        from pglast import ast, enums, parse_sql
    except ImportError as exc:
        raise CandidateGenerationError(
            "PostgreSQL workload analysis requires the pglast optional dependency"
        ) from exc
    return pglast, parse_sql, ast, enums, pglast.__version__


def parser_metadata() -> dict[str, str]:
    pglast, *_ = _pglast()
    return {
        "parser": "pglast",
        "parser_version": pglast.__version__,
        "analysis_contract_version": ANALYSIS_CONTRACT_VERSION,
    }


def _walk(node: Any) -> Iterable[Any]:
    from pglast.ast import Node

    if isinstance(node, Node):
        yield node
        for field in node:
            yield from _walk(getattr(node, field))
    elif isinstance(node, (list, tuple)):
        for item in node:
            yield from _walk(item)


def _column_ref(node: Any, aliases: set[str], schema: RelationSchema) -> int | None:
    from pglast.ast import ColumnRef, String

    if not isinstance(node, ColumnRef) or not node.fields:
        return None
    fields = node.fields
    if any(not isinstance(field, String) for field in fields):
        return None
    names = tuple(field.sval for field in fields)
    if len(names) == 1:
        column_name = names[0]
    elif len(names) == 2 and names[0] in aliases:
        column_name = names[1]
    else:
        return None
    for column in schema.columns:
        if column.name == column_name:
            return column.ordinal
    return None


def _is_value(node: Any, ast: Any) -> bool:
    if isinstance(node, (ast.A_Const, ast.ParamRef)):
        return True
    if isinstance(node, ast.TypeCast):
        return _is_value(node.arg, ast)
    return False


def _flatten_predicates(node: Any, enums: Any) -> tuple[Any, ...]:
    from pglast.ast import BoolExpr

    if isinstance(node, BoolExpr):
        if node.boolop != enums.BoolExprType.AND_EXPR:
            raise ValueError("predicate boolean structure is not a conjunction")
        result: list[Any] = []
        for arg in node.args:
            result.extend(_flatten_predicates(arg, enums))
        return tuple(result)
    return (node,)


def _predicate_column(node: Any, aliases: set[str], schema: RelationSchema, ast: Any) -> int | None:
    return _column_ref(node, aliases, schema) if isinstance(node, ast.ColumnRef) else None


def _extract_predicate_columns(
    where: Any, aliases: set[str], schema: RelationSchema, ast: Any, enums: Any
) -> tuple[int, ...]:
    if where is None:
        return ()
    ordinals: set[int] = set()
    for predicate in _flatten_predicates(where, enums):
        column_ordinal: int | None = None
        if isinstance(predicate, ast.A_Expr):
            if predicate.kind == enums.A_Expr_Kind.AEXPR_OP:
                if len(predicate.name) != 1 or not isinstance(predicate.name[0], ast.String):
                    raise ValueError("operator is not a supported scalar comparison")
                operator = predicate.name[0].sval
                if operator not in _SUPPORTED_OPERATORS:
                    raise ValueError("operator is outside the supported selection scope")
                left_column = _predicate_column(predicate.lexpr, aliases, schema, ast)
                right_column = _predicate_column(predicate.rexpr, aliases, schema, ast)
                if left_column is not None and _is_value(predicate.rexpr, ast):
                    column_ordinal = left_column
                elif right_column is not None and _is_value(predicate.lexpr, ast):
                    column_ordinal = right_column
                else:
                    raise ValueError("comparison must contain one column and one value")
            elif predicate.kind == enums.A_Expr_Kind.AEXPR_BETWEEN:
                if _predicate_column(predicate.lexpr, aliases, schema, ast) is None:
                    raise ValueError("BETWEEN requires a direct column")
                if len(predicate.rexpr) != 2 or not all(
                    _is_value(value, ast) for value in predicate.rexpr
                ):
                    raise ValueError("BETWEEN bounds must be values")
                column_ordinal = _predicate_column(predicate.lexpr, aliases, schema, ast)
            elif predicate.kind == enums.A_Expr_Kind.AEXPR_IN:
                column_ordinal = _predicate_column(predicate.lexpr, aliases, schema, ast)
                if (
                    column_ordinal is None
                    or not predicate.rexpr
                    or not all(_is_value(value, ast) for value in predicate.rexpr)
                ):
                    raise ValueError("IN requires one direct column and value list")
            else:
                raise ValueError("predicate expression is outside the supported scope")
        elif isinstance(predicate, ast.NullTest):
            if predicate.nulltesttype not in {
                enums.NullTestType.IS_NULL,
                enums.NullTestType.IS_NOT_NULL,
            }:
                raise ValueError("null test is outside the supported scope")
            column_ordinal = _predicate_column(predicate.arg, aliases, schema, ast)
            if column_ordinal is None:
                raise ValueError("null test requires a direct column")
        else:
            raise TypeError("predicate is outside the supported scope")
        ordinals.add(column_ordinal)
    return tuple(sorted(ordinals))


def _relation_binding(relation: Any, schema: RelationSchema, ast: Any) -> set[str]:
    if not isinstance(relation, ast.RangeVar):
        raise TypeError("joins and derived relations are unsupported")
    native = schema.relation_name
    if relation.catalogname is not None and relation.catalogname != native.catalog:
        raise ValueError("qualified relation catalog does not match snapshot")
    if relation.schemaname is not None and relation.schemaname != native.schema:
        raise ValueError("qualified relation schema does not match snapshot")
    if relation.relname != native.name:
        raise ValueError("workload relation does not match snapshot")
    aliases = {relation.alias.aliasname if relation.alias is not None else relation.relname}
    return aliases


def analyze_query(query: WorkloadQuery, schema: RelationSchema) -> PredicateProfile:
    try:
        pglast, parse_sql, ast, enums, _ = _pglast()
        statements = parse_sql(query.sql)
        if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
            raise ValueError("query must contain one SELECT statement")
        select = statements[0].stmt
        if select.withClause is not None:
            raise ValueError("CTEs are unsupported")
        if select.op != enums.SetOperation.SETOP_NONE:
            raise ValueError("set operations are unsupported")
        if not select.fromClause or len(select.fromClause) != 1:
            raise ValueError("query must reference exactly one base relation")
        aliases = _relation_binding(select.fromClause[0], schema, ast)
        if any(
            isinstance(node, (ast.SubLink, ast.RangeSubselect, ast.CommonTableExpr))
            for node in _walk(select)
        ):
            raise ValueError("subqueries are unsupported")
        ordinals = _extract_predicate_columns(select.whereClause, aliases, schema, ast, enums)
        names = tuple(
            next(column.name for column in schema.columns if column.ordinal == ordinal)
            for ordinal in ordinals
        )
        return PredicateProfile(
            query.query_id,
            schema.relation_id,
            query.weight,
            ordinals,
            names,
            SUPPORTED_ANALYSIS_STATUS,
        )
    except CandidateGenerationError:
        raise
    except (
        AttributeError,
        IndexError,
        KeyError,
        TypeError,
        ValueError,
        pglast.parser.ParseError,
    ) as exc:
        return PredicateProfile(
            query.query_id,
            None,
            query.weight,
            (),
            (),
            UNSUPPORTED_ANALYSIS_STATUS,
            reason=str(exc) or "query is outside the supported scope",
        )


def analyze_workload(workload: Workload, schema: RelationSchema) -> tuple[PredicateProfile, ...]:
    return tuple(analyze_query(query, schema) for query in workload.queries)
