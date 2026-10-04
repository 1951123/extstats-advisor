"""PostgreSQL native statistics capability expansion for candidate generation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from extstats_advisor.candidates.groups import RelevantGroup
from extstats_advisor.candidates.model import Candidate

POSTGRES_CAPABILITY_VERSION = "postgresql-extended-statistics-capability-v1"
POSTGRES_STATIC_PRECEDENCE_VERSION = "postgresql-static-precedence-v1"
POSTGRES_CANDIDATE_KINDS = ("postgresql.mcv", "postgresql.dependencies")


@dataclass(frozen=True, slots=True)
class PostgresStatisticsCapability:
    """Versioned native-kind capability used only after core group derivation."""

    capability_version: str = POSTGRES_CAPABILITY_VERSION
    supported_kinds: tuple[str, ...] = POSTGRES_CANDIDATE_KINDS
    supported_group_arity: int = 2
    static_precedence_policy: str = POSTGRES_STATIC_PRECEDENCE_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": "postgresql",
            "capability_version": self.capability_version,
            "supported_kinds": list(self.supported_kinds),
            "supported_group_arity": self.supported_group_arity,
            "static_precedence_policy": self.static_precedence_policy,
        }

    def expand(self, groups: Sequence[RelevantGroup]) -> tuple[Candidate, ...]:
        if any(len(group.column_ordinals) != self.supported_group_arity for group in groups):
            raise ValueError("PostgreSQL v1 capability supports pair groups only")
        return _expand_candidates(groups, self.supported_kinds)


def static_precedence_key(candidate: Candidate) -> tuple[object, ...]:
    kind_rank = POSTGRES_CANDIDATE_KINDS.index(candidate.kind)
    return (
        kind_rank,
        len(candidate.column_ordinals),
        candidate.column_ordinals,
        candidate.column_names,
        candidate.candidate_id,
    )


def _expand_candidates(
    groups: Sequence[RelevantGroup], kinds: Sequence[str]
) -> tuple[Candidate, ...]:
    pending = [
        Candidate.make(
            group.group_id,
            group.relation_id,
            kind,
            group.column_ordinals,
            group.column_names,
            1,
        )
        for group in groups
        for kind in kinds
    ]
    ordered = sorted(pending, key=static_precedence_key)
    return tuple(
        Candidate(
            candidate.candidate_id,
            candidate.group_id,
            candidate.relation_id,
            candidate.kind,
            candidate.column_ordinals,
            candidate.column_names,
            rank,
        )
        for rank, candidate in enumerate(ordered, start=1)
    )


def expand_postgres_candidates(groups: Sequence[RelevantGroup]) -> tuple[Candidate, ...]:
    return PostgresStatisticsCapability().expand(groups)


def postgres_capability_metadata() -> dict[str, object]:
    return PostgresStatisticsCapability().to_dict()
