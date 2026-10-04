"""Small DBMS acquisition boundary shared by concrete source backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from extstats_advisor.snapshot.model import PopulationMetadata, RelationSchema


@dataclass(frozen=True, slots=True)
class SamplePolicy:
    """Bounded sampling policy supplied to a concrete DBMS backend."""

    sample_rows: int
    seed: int | None = None
    candidate_row_limit_multiplier: int = 20

    def __post_init__(self) -> None:
        if self.sample_rows < 1:
            raise ValueError("sample_rows must be positive")
        if self.candidate_row_limit_multiplier < 1:
            raise ValueError("candidate_row_limit_multiplier must be positive")


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    """A native relation selector plus the sampling policy for one capture."""

    relation_selector: str
    sampling: SamplePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.relation_selector, str) or not self.relation_selector.strip():
            raise ValueError("relation_selector must be non-empty")


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
    def acquire(self, request: AcquisitionRequest) -> AcquisitionInputs:
        """Return schema, population metadata, and samples from one session."""
