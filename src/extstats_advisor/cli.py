"""Command-line interface for snapshot inspection and validation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from extstats_advisor import __version__
from extstats_advisor.candidates import (
    derive_candidate_universe,
    validate_candidate_universe,
    write_candidate_universe,
)
from extstats_advisor.dbms.base import AcquisitionRequest, SamplePolicy
from extstats_advisor.dbms.postgres import PostgresSnapshotAcquirer
from extstats_advisor.dbms.postgres.acquisition import _workload_from_path
from extstats_advisor.errors import ExtStatsAdvisorError
from extstats_advisor.snapshot.bundle import load_snapshot, validate_snapshot, write_snapshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="extstats-advisor")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    snapshot = commands.add_parser("snapshot")
    snapshot_commands = snapshot.add_subparsers(dest="snapshot_command", required=True)
    for name in ("validate", "inspect"):
        command = snapshot_commands.add_parser(name)
        command.add_argument("path", type=Path)
    capture = snapshot_commands.add_parser("capture")
    capture_commands = capture.add_subparsers(dest="capture_backend", required=True)
    postgres = capture_commands.add_parser("postgres")
    postgres.add_argument("--dsn", default=os.environ.get("EXTSTATS_ADVISOR_POSTGRES_DSN"))
    postgres.add_argument("--relation", required=True)
    postgres.add_argument("--sample-rows", required=True, type=int)
    postgres.add_argument("--sample-seed", type=int)
    postgres.add_argument("--workload", required=True, type=Path)
    postgres.add_argument("--output", required=True, type=Path)
    postgres.add_argument("--candidate-row-limit-multiplier", type=int, default=20)
    candidates = commands.add_parser("candidates")
    candidate_commands = candidates.add_subparsers(dest="candidate_command", required=True)
    derive = candidate_commands.add_parser("derive")
    derive.add_argument("snapshot", type=Path)
    derive.add_argument("--output", required=True, type=Path)
    for name in ("validate", "inspect"):
        command = candidate_commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--snapshot", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if getattr(args, "snapshot_command", None) == "capture":
            if not args.dsn:
                raise ExtStatsAdvisorError(
                    "PostgreSQL DSN is required via --dsn or EXTSTATS_ADVISOR_POSTGRES_DSN"
                )
            workload = _workload_from_path(args.workload)
            request = AcquisitionRequest(
                args.relation,
                SamplePolicy(
                    args.sample_rows,
                    seed=args.sample_seed,
                    candidate_row_limit_multiplier=args.candidate_row_limit_multiplier,
                ),
            )
            snapshot = PostgresSnapshotAcquirer(args.dsn).capture(request, workload)
            digest = write_snapshot(snapshot, args.output)
            print(
                json.dumps(
                    {
                        "status": "captured",
                        "semantic_digest": digest,
                        "output": str(args.output),
                        "relation_count": len(snapshot.schemas),
                        "sample_row_counts": {
                            relation_id: table.num_rows
                            for relation_id, table in snapshot.samples.items()
                        },
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "candidates":
            if args.candidate_command == "derive":
                snapshot = load_snapshot(args.snapshot)
                universe = derive_candidate_universe(snapshot)
                digest = write_candidate_universe(universe, args.output)
                print(
                    json.dumps(
                        {
                            "status": "derived",
                            "semantic_digest": digest,
                            "output": str(args.output),
                            "relevant_group_count": len(universe.relevant_groups),
                            "candidate_count": len(universe.candidates),
                            "incidence_count": len(universe.incidence),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            source_snapshot = load_snapshot(args.snapshot) if args.snapshot else None
            summary = validate_candidate_universe(args.path, source_snapshot)
            if args.candidate_command == "validate":
                print(
                    json.dumps(
                        {"status": "valid", **summary}, sort_keys=True, separators=(",", ":")
                    )
                )
            else:
                print(json.dumps(summary, sort_keys=True, indent=2))
            return 0
        summary = validate_snapshot(args.path)
    except (ExtStatsAdvisorError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.snapshot_command == "validate":
        print(json.dumps({"status": "valid", **summary}, sort_keys=True, separators=(",", ":")))
    else:
        print(
            json.dumps(
                {
                    "format_version": summary["format_version"],
                    "semantic_digest": summary["semantic_digest"],
                    "source_dbms": summary["dbms"],
                    "relation_count": summary["relation_count"],
                    "relations": summary["relations"],
                    "sample_row_counts": summary["sample_row_counts"],
                    "population": summary["population"],
                    "query_count": summary["query_count"],
                    "snapshot_consistency": summary["snapshot_consistency"],
                },
                sort_keys=True,
                indent=2,
            )
        )
    return 0
