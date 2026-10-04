"""Typed Apache Arrow IPC file serialization."""

from __future__ import annotations

import io

import pyarrow as pa

from extstats_advisor.errors import SnapshotValidationError


def serialize_table(table: pa.Table) -> bytes:
    if not isinstance(table, pa.Table):
        raise TypeError("sample must be a pyarrow.Table")
    output = io.BytesIO()
    try:
        with pa.ipc.new_file(output, table.schema) as writer:
            writer.write_table(table)
    except (pa.ArrowException, ValueError) as exc:
        raise SnapshotValidationError(f"could not serialize Arrow sample: {exc}") from exc
    return output.getvalue()


def deserialize_table(payload: bytes) -> pa.Table:
    if (
        not isinstance(payload, bytes)
        or len(payload) < 12
        or not payload.startswith(b"ARROW1")
        or not payload.endswith(b"ARROW1")
    ):
        raise SnapshotValidationError("sample is not a complete Arrow IPC file")
    try:
        with pa.ipc.open_file(pa.BufferReader(payload)) as reader:
            return reader.read_all()
    except (pa.ArrowException, ValueError, OSError) as exc:
        raise SnapshotValidationError(f"could not read Arrow sample: {exc}") from exc
