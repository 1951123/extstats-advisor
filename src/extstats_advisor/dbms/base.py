"""Minimal DBMS acquisition boundary for future concrete backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from extstats_advisor.snapshot.model import PopulationMetadata, RelationSchema


@dataclass(frozen=True, slots=True)
class SampleRequest:
    relation_id: str
    target_rows: int
    method: str
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class AcquisitionInputs:
    """The output of one consistent, read-only DBMS acquisition session."""

    schemas: tuple[RelationSchema, ...]
    populations: tuple[PopulationMetadata, ...]
    samples: Mapping[str, Any]
    provenance: Mapping[str, Any]


class DBMSAcquirer(ABC):
    """Acquire schema, population metadata, and typed samples without truths.

    A concrete implementation must perform all database reads needed for one
    ``acquire`` call inside one consistent read-only acquisition session. The
    generic contract deliberately says nothing about transactions, sampling
    syntax, catalogs, OIDs, or optimizer internals. The first concrete backend
    is planned to be PostgreSQL.
    """

    @abstractmethod
    def acquire(
        self, relations: Sequence[str], requests: Sequence[SampleRequest]
    ) -> AcquisitionInputs:
        """Return `(Σ, S, P)` from one read-only acquisition session."""
