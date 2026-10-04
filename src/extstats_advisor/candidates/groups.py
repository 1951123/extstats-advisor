"""DBMS-neutral structural relevant-group derivation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any

from extstats_advisor.canonical import digest_json
from extstats_advisor.errors import CandidateGenerationError
from extstats_advisor.snapshot.model import RelationSchema
from extstats_advisor.workload.analysis import (
    SUPPORTED_ANALYSIS_STATUS,
    PredicateProfile,
)


def _group_id(relation_id: str, ordinals: tuple[int, ...]) -> str:
    return (
        f"group_{digest_json({'relation_id': relation_id, 'column_ordinals': list(ordinals)})[:24]}"
    )


@dataclass(frozen=True, slots=True)
class RelevantGroup:
    group_id: str
    relation_id: str
    column_ordinals: tuple[int, ...]
    column_names: tuple[str, ...]
    supporting_query_ids: tuple[str, ...]
    supporting_weight: float

    def __post_init__(self) -> None:
        if not self.group_id.startswith("group_"):
            raise CandidateGenerationError("group ID must be opaque")
        if self.group_id != _group_id(self.relation_id, self.column_ordinals):
            raise CandidateGenerationError("relevant group ID does not match semantic identity")
        if (
            len(self.column_ordinals) != 2
            or tuple(sorted(self.column_ordinals)) != self.column_ordinals
        ):
            raise CandidateGenerationError("relevant groups must be canonical pairs")
        if len(self.column_names) != 2 or len(self.supporting_query_ids) < 1:
            raise CandidateGenerationError("relevant group evidence is incomplete")
        if self.supporting_weight < 0:
            raise CandidateGenerationError("relevant group weight cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "relation_id": self.relation_id,
            "column_ordinals": list(self.column_ordinals),
            "column_names": list(self.column_names),
            "supporting_query_ids": list(self.supporting_query_ids),
            "supporting_weight": self.supporting_weight,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> RelevantGroup:
        try:
            return cls(
                value["group_id"],
                value["relation_id"],
                tuple(value["column_ordinals"]),
                tuple(value["column_names"]),
                tuple(value["supporting_query_ids"]),
                value["supporting_weight"],
            )
        except (KeyError, TypeError) as exc:
            raise CandidateGenerationError("invalid relevant group") from exc


def derive_relevant_groups(
    schema: RelationSchema, predicate_profiles: Sequence[PredicateProfile]
) -> tuple[RelevantGroup, ...]:
    columns = {column.ordinal: column.name for column in schema.columns}
    evidence: dict[tuple[int, int], dict[str, Any]] = {}
    for profile in predicate_profiles:
        if profile.relation_id not in {None, schema.relation_id}:
            if profile.weight > 0:
                raise CandidateGenerationError(
                    f"query {profile.query_id}: workload relation does not match snapshot relation"
                )
            continue
        if profile.analysis_status != SUPPORTED_ANALYSIS_STATUS:
            if profile.weight > 0:
                raise CandidateGenerationError(
                    f"query {profile.query_id}: {profile.reason or 'unsupported workload query'}"
                )
            continue
        if profile.relation_id != schema.relation_id:
            raise CandidateGenerationError(
                f"query {profile.query_id}: supported profile is not bound to the snapshot relation"
            )
        if any(ordinal not in columns for ordinal in profile.predicate_column_ordinals):
            raise CandidateGenerationError(f"query {profile.query_id}: unknown snapshot column")
        expected_names = tuple(columns[ordinal] for ordinal in profile.predicate_column_ordinals)
        if expected_names != profile.predicate_column_names:
            raise CandidateGenerationError(f"query {profile.query_id}: predicate column mismatch")
        for pair in combinations(profile.predicate_column_ordinals, 2):
            item = evidence.setdefault(pair, {"queries": set(), "weight": 0.0})
            item["queries"].add(profile.query_id)
            item["weight"] += profile.weight
    result = []
    for ordinals in sorted(evidence):
        item = evidence[ordinals]
        result.append(
            RelevantGroup(
                _group_id(schema.relation_id, ordinals),
                schema.relation_id,
                ordinals,
                tuple(columns[ordinal] for ordinal in ordinals),
                tuple(sorted(item["queries"])),
                item["weight"],
            )
        )
    return tuple(result)
