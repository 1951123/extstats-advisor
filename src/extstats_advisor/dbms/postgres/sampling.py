"""Deterministic bounded PostgreSQL SYSTEM sampling primitives."""

from __future__ import annotations

import random
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from extstats_advisor.dbms.postgres.errors import SampleAcquisitionError, SamplingResourceLimitError

SAMPLING_METHOD = "postgresql-system-adaptive-v1"


def initial_percentage(sample_rows: int, population_rows: float) -> float:
    if sample_rows < 1 or population_rows <= 0:
        raise ValueError("sample_rows and population_rows must be positive")
    return min(100.0, max(0.01, 100.0 * (2.0 * sample_rows / population_rows)))


def next_percentage(current: float) -> float:
    if not 0 < current <= 100:
        raise ValueError("sampling percentage must be in (0, 100]")
    return min(100.0, max(current + 0.01, current * 2.0))


def deterministic_reservoir[T](
    rows: Iterable[T], target_rows: int, seed: int, *, candidate_limit: int
) -> tuple[tuple[T, ...], int]:
    """Keep exactly target_rows when possible, without retaining the candidate stream."""

    if target_rows < 1 or candidate_limit < target_rows:
        raise ValueError("invalid reservoir bounds")
    rng = random.Random(seed)
    reservoir: list[T] = []
    seen = 0
    try:
        for row in rows:
            seen += 1
            if seen > candidate_limit:
                raise SamplingResourceLimitError(
                    "sampling candidate stream exceeded the configured resource limit; "
                    "the PostgreSQL reltuples estimate may be stale or the sample percentage too aggressive"
                )
            if len(reservoir) < target_rows:
                reservoir.append(row)
            else:
                slot = rng.randrange(seen)
                if slot < target_rows:
                    reservoir[slot] = row
    except SamplingResourceLimitError:
        raise
    except Exception as exc:
        raise SampleAcquisitionError("could not stream PostgreSQL sample rows") from exc
    return tuple(reservoir), seen


@dataclass(frozen=True, slots=True)
class SamplingAttempt:
    percentage: float
    candidate_rows: int
    retained_rows: int


def sample_percentages[T](
    sample_rows: int,
    population_rows: float,
    candidate_streams: Iterable[Iterable[T]],
    *,
    seed: int,
    candidate_limit: int,
) -> tuple[tuple[T, ...], float, tuple[SamplingAttempt, ...]]:
    """Apply monotonic retries; each supplied stream represents a fresh DB sample."""

    percentage = initial_percentage(sample_rows, population_rows)
    attempts: list[SamplingAttempt] = []
    for stream in candidate_streams:
        rows, candidate_rows = deterministic_reservoir(
            stream, sample_rows, seed, candidate_limit=candidate_limit
        )
        attempts.append(SamplingAttempt(percentage, candidate_rows, len(rows)))
        if len(rows) >= sample_rows or percentage >= 100.0:
            if not rows:
                raise SampleAcquisitionError("the relation contains no visible rows")
            return rows, percentage, tuple(attempts)
        percentage = next_percentage(percentage)
    raise SampleAcquisitionError("sampling retry stream ended before a sufficient sample")


def iter_batches(cursor: object, batch_size: int = 256) -> Iterator[tuple[object, ...]]:
    """Yield bounded fetchmany batches from a psycopg server-side cursor."""

    while True:
        batch = cursor.fetchmany(batch_size)  # type: ignore[attr-defined]
        if not batch:
            return
        yield from batch
