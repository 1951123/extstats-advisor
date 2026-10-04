"""DBMS-neutral candidate and structural-incidence models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from extstats_advisor.canonical import digest_json
from extstats_advisor.errors import CandidateGenerationError


def _candidate_id(relation_id: str, kind: str, ordinals: tuple[int, ...]) -> str:
    return f"cand_{digest_json({'relation_id': relation_id, 'kind': kind, 'column_ordinals': list(ordinals)})[:24]}"


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    group_id: str
    relation_id: str
    kind: str
    column_ordinals: tuple[int, ...]
    column_names: tuple[str, ...]
    static_precedence_rank: int

    def __post_init__(self) -> None:
        if not self.candidate_id.startswith("cand_"):
            raise CandidateGenerationError("candidate ID must be opaque")
        if not self.group_id.startswith("group_"):
            raise CandidateGenerationError("candidate group ID must be opaque")
        if not self.relation_id or not isinstance(self.kind, str) or not self.kind:
            raise CandidateGenerationError("invalid candidate identity")
        if self.candidate_id != _candidate_id(self.relation_id, self.kind, self.column_ordinals):
            raise CandidateGenerationError("candidate ID does not match semantic identity")
        if (
            len(self.column_ordinals) != 2
            or tuple(sorted(self.column_ordinals)) != self.column_ordinals
        ):
            raise CandidateGenerationError("candidate columns must be a canonical pair")
        if len(self.column_names) != 2 or any(not name for name in self.column_names):
            raise CandidateGenerationError("candidate column names must match the pair")
        if self.static_precedence_rank < 1:
            raise CandidateGenerationError("candidate precedence rank must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "group_id": self.group_id,
            "relation_id": self.relation_id,
            "kind": self.kind,
            "column_ordinals": list(self.column_ordinals),
            "column_names": list(self.column_names),
            "static_precedence_rank": self.static_precedence_rank,
        }

    @classmethod
    def make(
        cls,
        group_id: str,
        relation_id: str,
        kind: str,
        column_ordinals: tuple[int, ...],
        column_names: tuple[str, ...],
        static_precedence_rank: int,
    ) -> Candidate:
        return cls(
            _candidate_id(relation_id, kind, column_ordinals),
            group_id,
            relation_id,
            kind,
            column_ordinals,
            column_names,
            static_precedence_rank,
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Candidate:
        try:
            return cls(
                value["candidate_id"],
                value["group_id"],
                value["relation_id"],
                value["kind"],
                tuple(value["column_ordinals"]),
                tuple(value["column_names"]),
                value["static_precedence_rank"],
            )
        except (KeyError, TypeError) as exc:
            raise CandidateGenerationError("invalid candidate") from exc


@dataclass(frozen=True, slots=True)
class Incidence:
    query_id: str
    candidate_id: str

    def to_dict(self) -> dict[str, str]:
        return {"query_id": self.query_id, "candidate_id": self.candidate_id}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Incidence:
        try:
            return cls(value["query_id"], value["candidate_id"])
        except (KeyError, TypeError) as exc:
            raise CandidateGenerationError("invalid candidate incidence") from exc
