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
    load_candidate_universe,
    validate_candidate_universe,
    write_candidate_universe,
)
from extstats_advisor.dbms.base import AcquisitionRequest, SamplePolicy
from extstats_advisor.dbms.postgres import (
    PostgresPlannerSession,
    PostgresSnapshotAcquirer,
    PostgresStatisticsConfiguration,
    destroy_postgres_planner_sandbox,
    prepare_postgres_planner_sandbox,
    verify_postgres_planner_sandbox,
)
from extstats_advisor.dbms.postgres.acquisition import _workload_from_path
from extstats_advisor.errors import ExtStatsAdvisorError
from extstats_advisor.ground_truth import (
    ArtifactGroundTruthProvider,
    load_ground_truth_set,
    write_ground_truth_set,
)
from extstats_advisor.native_stats.repository import load_native_stats_repository
from extstats_advisor.snapshot.bundle import load_snapshot, validate_snapshot, write_snapshot
from extstats_advisor.utility import QErrorLoss, WeightedWorkloadUtility


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
    postgres.add_argument("--lock-timeout-ms", type=int, default=5_000)
    postgres.add_argument("--statement-timeout-ms", type=int, default=60_000)
    postgres.add_argument("--ground-truth-output", type=Path)
    candidates = commands.add_parser("candidates")
    candidate_commands = candidates.add_subparsers(dest="candidate_command", required=True)
    derive = candidate_commands.add_parser("derive")
    derive.add_argument("snapshot", type=Path)
    derive.add_argument("--output", required=True, type=Path)
    for name in ("validate", "inspect"):
        command = candidate_commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--snapshot", type=Path)
    sandbox = commands.add_parser("sandbox")
    sandbox_commands = sandbox.add_subparsers(dest="sandbox_command", required=True)
    for name in ("prepare", "verify"):
        command = sandbox_commands.add_parser(name)
        backend = command.add_subparsers(dest="sandbox_backend", required=True)
        postgres = backend.add_parser("postgres")
        postgres.add_argument("snapshot", type=Path)
        postgres.add_argument("candidate_universe", type=Path)
        postgres.add_argument("native_repository", type=Path)
        postgres.add_argument(
            "--dsn", default=os.environ.get("EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN")
        )
    destroy = sandbox_commands.add_parser("destroy")
    destroy_backend = destroy.add_subparsers(dest="sandbox_backend", required=True)
    destroy_postgres = destroy_backend.add_parser("postgres")
    destroy_postgres.add_argument(
        "--dsn", default=os.environ.get("EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN")
    )
    planner = commands.add_parser("planner")
    planner_commands = planner.add_subparsers(dest="planner_command", required=True)
    estimate = planner_commands.add_parser("estimate")
    estimate_backend = estimate.add_subparsers(dest="planner_backend", required=True)
    estimate_postgres = estimate_backend.add_parser("postgres")
    estimate_postgres.add_argument("snapshot", type=Path)
    estimate_postgres.add_argument("candidate_universe", type=Path)
    estimate_postgres.add_argument("native_repository", type=Path)
    estimate_postgres.add_argument(
        "--dsn", default=os.environ.get("EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN")
    )
    estimate_postgres.add_argument("--query-id", required=True)
    estimate_postgres.add_argument("--candidate", action="append", default=[])
    utility = commands.add_parser("utility")
    utility_commands = utility.add_subparsers(dest="utility_command", required=True)
    evaluate = utility_commands.add_parser("evaluate")
    evaluate_backend = evaluate.add_subparsers(dest="utility_backend", required=True)
    evaluate_postgres = evaluate_backend.add_parser("postgres")
    evaluate_postgres.add_argument("snapshot", type=Path)
    evaluate_postgres.add_argument("candidate_universe", type=Path)
    evaluate_postgres.add_argument("native_repository", type=Path)
    evaluate_postgres.add_argument("ground_truth", type=Path)
    evaluate_postgres.add_argument(
        "--dsn", default=os.environ.get("EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN")
    )
    evaluate_postgres.add_argument("--candidate", action="append", default=[])
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
            acquirer = PostgresSnapshotAcquirer(
                args.dsn,
                lock_timeout_ms=args.lock_timeout_ms,
                statement_timeout_ms=args.statement_timeout_ms,
            )
            truth_digest = None
            if args.ground_truth_output is None:
                snapshot = acquirer.capture(request, workload)
            else:
                snapshot, ground_truth = acquirer.capture_with_ground_truth(request, workload)
            digest = write_snapshot(snapshot, args.output)
            if args.ground_truth_output is not None:
                if ground_truth.source_snapshot_semantic_digest != digest:
                    raise ExtStatsAdvisorError(
                        "ground-truth snapshot digest does not match published snapshot"
                    )
                truth_digest = write_ground_truth_set(ground_truth, args.ground_truth_output)
            print(
                json.dumps(
                    {
                        "status": "captured",
                        "semantic_digest": digest,
                        "output": str(args.output),
                        "relation_count": len(snapshot.schemas),
                        "ground_truth_semantic_digest": truth_digest,
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
        if args.command == "sandbox":
            if not args.dsn:
                raise ExtStatsAdvisorError(
                    "patched PostgreSQL DSN is required via --dsn or "
                    "EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN"
                )
            if args.sandbox_command == "destroy":
                print(json.dumps(destroy_postgres_planner_sandbox(args.dsn), sort_keys=True))
                return 0
            snapshot = load_snapshot(args.snapshot)
            universe = load_candidate_universe(args.candidate_universe, snapshot)
            repository = load_native_stats_repository(args.native_repository)
            if args.sandbox_command == "prepare":
                prepared = prepare_postgres_planner_sandbox(
                    args.dsn, snapshot, universe, repository
                )
                print(
                    json.dumps(
                        {"status": "prepared", **prepared.metadata.to_dict()},
                        sort_keys=True,
                    )
                )
            else:
                print(
                    json.dumps(
                        {
                            "status": "valid",
                            **verify_postgres_planner_sandbox(
                                args.dsn, snapshot, universe, repository
                            ),
                        },
                        sort_keys=True,
                    )
                )
            return 0
        if args.command == "planner":
            if not args.dsn:
                raise ExtStatsAdvisorError(
                    "patched PostgreSQL DSN is required via --dsn or "
                    "EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN"
                )
            snapshot = load_snapshot(args.snapshot)
            universe = load_candidate_universe(args.candidate_universe, snapshot)
            repository = load_native_stats_repository(args.native_repository)
            with PostgresPlannerSession(args.dsn, snapshot, universe, repository) as session:
                configuration = PostgresStatisticsConfiguration(tuple(args.candidate))
                session.activate(configuration)
                result = session.estimate_query(args.query_id)
            print(
                json.dumps(
                    {
                        "query_id": result.query_id,
                        "ordered_candidate_ids": list(configuration.ordered_candidate_ids),
                        "estimated_rows": result.estimated_rows,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "utility":
            if not args.dsn:
                raise ExtStatsAdvisorError(
                    "patched PostgreSQL DSN is required via --dsn or "
                    "EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN"
                )
            snapshot = load_snapshot(args.snapshot)
            universe = load_candidate_universe(args.candidate_universe, snapshot)
            repository = load_native_stats_repository(args.native_repository)
            ground_truth = load_ground_truth_set(args.ground_truth, snapshot)
            supported_query_ids = []
            profiles = {profile.query_id: profile for profile in universe.query_profiles}
            for query in snapshot.workload.queries:
                if query.weight > 0:
                    profile = profiles.get(query.query_id)
                    if profile is None or profile.analysis_status != "supported":
                        raise ExtStatsAdvisorError(
                            f"positive-weight query {query.query_id!r} is outside utility scope"
                        )
                    supported_query_ids.append(query.query_id)
            with PostgresPlannerSession(args.dsn, snapshot, universe, repository) as session:
                configuration = PostgresStatisticsConfiguration(tuple(args.candidate))
                session.activate(configuration)
                planner_estimates = session.estimate_queries(supported_query_ids)
            estimates = {
                estimate.query_id: estimate.estimated_rows for estimate in planner_estimates
            }
            result = WeightedWorkloadUtility(
                snapshot.workload,
                ArtifactGroundTruthProvider(ground_truth),
                QErrorLoss(),
            ).evaluate(estimates)
            print(
                json.dumps(
                    {
                        "ordered_candidate_ids": list(configuration.ordered_candidate_ids),
                        **result.to_dict(),
                    },
                    sort_keys=True,
                )
            )
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
