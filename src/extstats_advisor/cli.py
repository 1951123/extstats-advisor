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
    DEFAULT_LOCK_TIMEOUT_MS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    PostgresPlannerSession,
    PostgresSnapshotAcquirer,
    PostgresStatisticsConfiguration,
    build_postgres_recommendation,
    deploy_postgres_recommendation,
    destroy_postgres_planner_sandbox,
    preflight_postgres_recommendation,
    prepare_postgres_planner_sandbox,
    profile_postgres_singletons,
    render_postgres_sql,
    search_postgres_greedy_add,
    verify_postgres_planner_sandbox,
)
from extstats_advisor.dbms.postgres.acquisition import _workload_from_path
from extstats_advisor.deployment.artifact import (
    inspect_deployment_result,
    validate_deployment_result,
    write_deployment_result,
)
from extstats_advisor.errors import ExtStatsAdvisorError
from extstats_advisor.ground_truth import (
    ArtifactGroundTruthProvider,
    import_authoritative_ground_truth,
    inspect_ground_truth,
    load_ground_truth_set,
    validate_ground_truth_set,
    write_ground_truth_set,
)
from extstats_advisor.native_stats.repository import load_native_stats_repository
from extstats_advisor.optimization.artifact import (
    inspect_singleton_profile,
    load_singleton_profile,
    validate_singleton_profile,
    write_singleton_profile,
)
from extstats_advisor.optimization.plan import create_optimization_plan
from extstats_advisor.optimization.plan_artifact import (
    inspect_optimization_plan,
    load_optimization_plan,
    validate_optimization_plan,
    write_optimization_plan,
)
from extstats_advisor.optimization.search_artifact import (
    inspect_search_result,
    load_search_result,
    validate_search_result,
    write_search_result,
)
from extstats_advisor.recommendation.artifact import (
    inspect_recommendation,
    load_recommendation,
    validate_recommendation,
    write_recommendation,
)
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
    ground_truth = commands.add_parser("ground-truth", aliases=["ground_truth"])
    ground_truth_commands = ground_truth.add_subparsers(dest="ground_truth_command", required=True)
    import_authoritative = ground_truth_commands.add_parser("import")
    import_authoritative_commands = import_authoritative.add_subparsers(
        dest="ground_truth_import_kind", required=True
    )
    authoritative = import_authoritative_commands.add_parser("authoritative")
    authoritative.add_argument("snapshot", type=Path)
    authoritative.add_argument("observations", type=Path)
    authoritative.add_argument("--authority", required=True)
    authoritative.add_argument("--dataset-identity", required=True)
    authoritative.add_argument("--source-revision", required=True)
    authoritative.add_argument("--output", required=True, type=Path)
    for name in ("validate", "inspect"):
        command = ground_truth_commands.add_parser(name)
        command.add_argument("path", type=Path)
        command.add_argument("--snapshot", type=Path)
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
    profiling = commands.add_parser("profiling")
    profiling_commands = profiling.add_subparsers(dest="profiling_command", required=True)
    singleton = profiling_commands.add_parser("singleton")
    singleton_backend = singleton.add_subparsers(dest="profiling_backend", required=True)
    singleton_postgres = singleton_backend.add_parser("postgres")
    singleton_postgres.add_argument("snapshot", type=Path)
    singleton_postgres.add_argument("candidate_universe", type=Path)
    singleton_postgres.add_argument("native_repository", type=Path)
    singleton_postgres.add_argument("ground_truth", type=Path)
    singleton_postgres.add_argument(
        "--dsn", default=os.environ.get("EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN")
    )
    singleton_postgres.add_argument("--output", required=True, type=Path)
    validate_profile = profiling_commands.add_parser("validate")
    validate_profile.add_argument("profile", type=Path)
    validate_profile.add_argument("--snapshot", required=True, type=Path)
    validate_profile.add_argument("--candidate-universe", required=True, type=Path)
    validate_profile.add_argument("--native-repository", required=True, type=Path)
    validate_profile.add_argument("--ground-truth", required=True, type=Path)
    inspect_profile = profiling_commands.add_parser("inspect")
    inspect_profile.add_argument("profile", type=Path)
    optimization = commands.add_parser("optimization")
    optimization_commands = optimization.add_subparsers(dest="optimization_command", required=True)
    plan = optimization_commands.add_parser("plan")
    plan.add_argument("snapshot", type=Path)
    plan.add_argument("candidate_universe", type=Path)
    plan.add_argument("native_repository", type=Path)
    plan.add_argument("ground_truth", type=Path)
    plan.add_argument("singleton_profile", type=Path)
    plan.add_argument("--candidate-limit", required=True, type=int)
    plan.add_argument(
        "--max-statistics-count",
        type=int,
        help="maximum selected candidate definitions B (defaults to candidate-limit)",
    )
    plan.add_argument("--wall-clock-seconds", default=300.0, type=float)
    plan.add_argument("--output", required=True, type=Path)
    validate_plan = optimization_commands.add_parser("validate")
    validate_plan.add_argument("optimization_plan", type=Path)
    validate_plan.add_argument("--snapshot", required=True, type=Path)
    validate_plan.add_argument("--candidate-universe", required=True, type=Path)
    validate_plan.add_argument("--native-repository", required=True, type=Path)
    validate_plan.add_argument("--ground-truth", required=True, type=Path)
    validate_plan.add_argument("--singleton-profile", required=True, type=Path)
    inspect_plan = optimization_commands.add_parser("inspect")
    inspect_plan.add_argument("optimization_plan", type=Path)
    search = optimization_commands.add_parser("search")
    search_commands = search.add_subparsers(dest="search_command", required=True)
    search_postgres = search_commands.add_parser("postgres")
    search_postgres.add_argument("snapshot", type=Path)
    search_postgres.add_argument("candidate_universe", type=Path)
    search_postgres.add_argument("native_repository", type=Path)
    search_postgres.add_argument("ground_truth", type=Path)
    search_postgres.add_argument("singleton_profile", type=Path)
    search_postgres.add_argument("optimization_plan", type=Path)
    search_postgres.add_argument(
        "--dsn", default=os.environ.get("EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN")
    )
    search_postgres.add_argument("--output", required=True, type=Path)
    validate_search = search_commands.add_parser("validate")
    validate_search.add_argument("search_result", type=Path)
    validate_search.add_argument("--snapshot", required=True, type=Path)
    validate_search.add_argument("--candidate-universe", required=True, type=Path)
    validate_search.add_argument("--native-repository", required=True, type=Path)
    validate_search.add_argument("--ground-truth", required=True, type=Path)
    validate_search.add_argument("--singleton-profile", required=True, type=Path)
    validate_search.add_argument("--optimization-plan", required=True, type=Path)
    inspect_search = search_commands.add_parser("inspect")
    inspect_search.add_argument("search_result", type=Path)
    recommendation = commands.add_parser("recommendation")
    recommendation_commands = recommendation.add_subparsers(
        dest="recommendation_command", required=True
    )
    build_recommendation = recommendation_commands.add_parser("build")
    build_backend = build_recommendation.add_subparsers(
        dest="recommendation_backend", required=True
    )
    build_postgres = build_backend.add_parser("postgres")
    build_postgres.add_argument("snapshot", type=Path)
    build_postgres.add_argument("candidate_universe", type=Path)
    build_postgres.add_argument("native_repository", type=Path)
    build_postgres.add_argument("ground_truth", type=Path)
    build_postgres.add_argument("singleton_profile", type=Path)
    build_postgres.add_argument("optimization_plan", type=Path)
    build_postgres.add_argument("search_result", type=Path)
    build_postgres.add_argument("--output", required=True, type=Path)
    validate_recommendation_command = recommendation_commands.add_parser("validate")
    validate_recommendation_command.add_argument("recommendation", type=Path)
    validate_recommendation_command.add_argument("--snapshot", required=True, type=Path)
    validate_recommendation_command.add_argument("--candidate-universe", required=True, type=Path)
    validate_recommendation_command.add_argument("--native-repository", required=True, type=Path)
    validate_recommendation_command.add_argument("--ground-truth", required=True, type=Path)
    validate_recommendation_command.add_argument("--singleton-profile", required=True, type=Path)
    validate_recommendation_command.add_argument("--optimization-plan", required=True, type=Path)
    validate_recommendation_command.add_argument("--search-result", required=True, type=Path)
    inspect_recommendation_command = recommendation_commands.add_parser("inspect")
    inspect_recommendation_command.add_argument("recommendation", type=Path)
    sql_recommendation = recommendation_commands.add_parser("sql")
    sql_recommendation.add_argument("recommendation", type=Path)
    deployment = commands.add_parser("deployment")
    deployment_commands = deployment.add_subparsers(dest="deployment_command", required=True)

    def add_deployment_sources(command: argparse.ArgumentParser) -> None:
        command.add_argument("snapshot", type=Path)
        command.add_argument("candidate_universe", type=Path)
        command.add_argument("native_repository", type=Path)
        command.add_argument("ground_truth", type=Path)
        command.add_argument("singleton_profile", type=Path)
        command.add_argument("optimization_plan", type=Path)
        command.add_argument("search_result", type=Path)
        command.add_argument("recommendation", type=Path)
        command.add_argument("--dsn", default=os.environ.get("EXTSTATS_ADVISOR_POSTGRES_DSN"))
        command.add_argument("--lock-timeout-ms", type=int, default=DEFAULT_LOCK_TIMEOUT_MS)
        command.add_argument(
            "--statement-timeout-ms", type=int, default=DEFAULT_STATEMENT_TIMEOUT_MS
        )

    apply_deployment = deployment_commands.add_parser("apply")
    apply_backend = apply_deployment.add_subparsers(dest="deployment_backend", required=True)
    apply_postgres = apply_backend.add_parser("postgres")
    add_deployment_sources(apply_postgres)
    apply_postgres.add_argument("--output", required=True, type=Path)
    preflight_deployment = deployment_commands.add_parser("preflight")
    preflight_backend = preflight_deployment.add_subparsers(
        dest="deployment_backend", required=True
    )
    preflight_postgres = preflight_backend.add_parser("postgres")
    add_deployment_sources(preflight_postgres)
    validate_deployment = deployment_commands.add_parser("validate")
    validate_deployment.add_argument("deployment_result", type=Path)
    validate_deployment.add_argument("recommendation", type=Path)
    validate_deployment.add_argument("--snapshot", required=True, type=Path)
    validate_deployment.add_argument("--candidate-universe", required=True, type=Path)
    validate_deployment.add_argument("--native-repository", required=True, type=Path)
    validate_deployment.add_argument("--ground-truth", required=True, type=Path)
    validate_deployment.add_argument("--singleton-profile", required=True, type=Path)
    validate_deployment.add_argument("--optimization-plan", required=True, type=Path)
    validate_deployment.add_argument("--search-result", required=True, type=Path)
    inspect_deployment = deployment_commands.add_parser("inspect")
    inspect_deployment.add_argument("deployment_result", type=Path)
    return parser


def _load_deployment_sources(args: argparse.Namespace) -> tuple[object, ...]:
    snapshot = load_snapshot(args.snapshot)
    universe = load_candidate_universe(args.candidate_universe, snapshot)
    repository = load_native_stats_repository(args.native_repository)
    ground_truth = load_ground_truth_set(args.ground_truth, snapshot)
    singleton_profile = load_singleton_profile(args.singleton_profile)
    optimization_plan = load_optimization_plan(args.optimization_plan)
    search_result = load_search_result(args.search_result)
    validate_singleton_profile(args.singleton_profile, snapshot, universe, repository, ground_truth)
    validate_optimization_plan(
        args.optimization_plan,
        snapshot,
        universe,
        repository,
        ground_truth,
        singleton_profile,
    )
    validate_search_result(
        args.search_result,
        snapshot,
        universe,
        repository,
        ground_truth,
        singleton_profile,
        optimization_plan,
    )
    recommendation = load_recommendation(args.recommendation)
    validate_recommendation(
        args.recommendation,
        snapshot,
        universe,
        repository,
        ground_truth,
        singleton_profile,
        optimization_plan,
        search_result,
    )
    return (
        snapshot,
        universe,
        repository,
        ground_truth,
        singleton_profile,
        optimization_plan,
        search_result,
        recommendation,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command in {"ground-truth", "ground_truth"}:
            if args.ground_truth_command == "import":
                ground_truth = import_authoritative_ground_truth(
                    args.snapshot,
                    args.observations,
                    authority=args.authority,
                    dataset_identity=args.dataset_identity,
                    source_revision=args.source_revision,
                )
                digest = write_ground_truth_set(ground_truth, args.output)
                print(
                    json.dumps(
                        {
                            "status": "imported",
                            "output": str(args.output),
                            **inspect_ground_truth(args.output),
                            "semantic_digest": digest,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            snapshot = load_snapshot(args.snapshot) if args.snapshot else None
            summary = validate_ground_truth_set(args.path, snapshot)
            if args.ground_truth_command == "validate":
                print(json.dumps({"status": "valid", **summary}, sort_keys=True))
            else:
                print(json.dumps(summary, sort_keys=True, indent=2))
            return 0
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
        if args.command == "deployment":
            if args.deployment_command == "inspect":
                print(
                    json.dumps(
                        inspect_deployment_result(args.deployment_result),
                        sort_keys=True,
                        indent=2,
                    )
                )
                return 0
            sources = _load_deployment_sources(args)
            (
                snapshot,
                universe,
                repository,
                ground_truth,
                singleton_profile,
                optimization_plan,
                search_result,
                recommendation,
            ) = sources
            if args.deployment_command == "validate":
                print(
                    json.dumps(
                        {
                            "status": "valid",
                            **validate_deployment_result(
                                args.deployment_result,
                                recommendation,
                                source_snapshot=snapshot,
                                candidate_universe=universe,
                                native_repository=repository,
                                singleton_profile=singleton_profile,
                                optimization_plan=optimization_plan,
                                search_result=search_result,
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            if args.deployment_command == "preflight":
                report = preflight_postgres_recommendation(
                    args.dsn,
                    snapshot,
                    universe,
                    repository,
                    ground_truth,
                    singleton_profile,
                    optimization_plan,
                    search_result,
                    recommendation,
                    lock_timeout_ms=args.lock_timeout_ms,
                    statement_timeout_ms=args.statement_timeout_ms,
                )
                print(json.dumps({"status": "ready", **report}, sort_keys=True))
                return 0
            result = deploy_postgres_recommendation(
                args.dsn,
                snapshot,
                universe,
                repository,
                ground_truth,
                singleton_profile,
                optimization_plan,
                search_result,
                recommendation,
                lock_timeout_ms=args.lock_timeout_ms,
                statement_timeout_ms=args.statement_timeout_ms,
            )
            digest = write_deployment_result(result, args.output)
            print(
                json.dumps(
                    {
                        "status": "deployed",
                        "output": str(args.output),
                        "semantic_digest": digest,
                        **inspect_deployment_result(args.output),
                    },
                    sort_keys=True,
                )
            )
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
        if args.command == "profiling":
            if args.profiling_command == "inspect":
                print(json.dumps(inspect_singleton_profile(args.profile), sort_keys=True, indent=2))
                return 0
            snapshot = load_snapshot(args.snapshot)
            universe = load_candidate_universe(args.candidate_universe, snapshot)
            repository = load_native_stats_repository(args.native_repository)
            ground_truth = load_ground_truth_set(args.ground_truth, snapshot)
            if args.profiling_command == "validate":
                print(
                    json.dumps(
                        {
                            "status": "valid",
                            **validate_singleton_profile(
                                args.profile, snapshot, universe, repository, ground_truth
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            if not args.dsn:
                raise ExtStatsAdvisorError(
                    "patched PostgreSQL DSN is required via --dsn or "
                    "EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN"
                )
            utility_provider = WeightedWorkloadUtility(
                snapshot.workload,
                ArtifactGroundTruthProvider(ground_truth),
                QErrorLoss(),
            )
            with PostgresPlannerSession(args.dsn, snapshot, universe, repository) as session:
                profile = profile_postgres_singletons(
                    session,
                    snapshot,
                    universe,
                    repository,
                    utility_provider,
                    ground_truth_semantic_digest=ground_truth.semantic_digest
                    or ground_truth.computed_semantic_digest,
                )
            digest = write_singleton_profile(profile, args.output)
            print(
                json.dumps(
                    {
                        "status": "profiled",
                        "output": str(args.output),
                        **inspect_singleton_profile(args.output),
                        "semantic_digest": digest,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "recommendation":
            if args.recommendation_command == "inspect":
                print(
                    json.dumps(
                        inspect_recommendation(args.recommendation), sort_keys=True, indent=2
                    )
                )
                return 0
            if args.recommendation_command == "sql":
                print(render_postgres_sql(load_recommendation(args.recommendation)), end="")
                return 0
            snapshot = load_snapshot(args.snapshot)
            universe = load_candidate_universe(args.candidate_universe, snapshot)
            repository = load_native_stats_repository(args.native_repository)
            ground_truth = load_ground_truth_set(args.ground_truth, snapshot)
            singleton_profile = load_singleton_profile(args.singleton_profile)
            optimization_plan = load_optimization_plan(args.optimization_plan)
            search_result = load_search_result(args.search_result)
            validate_singleton_profile(
                args.singleton_profile, snapshot, universe, repository, ground_truth
            )
            validate_optimization_plan(
                args.optimization_plan,
                snapshot,
                universe,
                repository,
                ground_truth,
                singleton_profile,
            )
            validate_search_result(
                args.search_result,
                snapshot,
                universe,
                repository,
                ground_truth,
                singleton_profile,
                optimization_plan,
            )
            if args.recommendation_command == "validate":
                print(
                    json.dumps(
                        {
                            "status": "valid",
                            **validate_recommendation(
                                args.recommendation,
                                snapshot,
                                universe,
                                repository,
                                ground_truth,
                                singleton_profile,
                                optimization_plan,
                                search_result,
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            recommendation = build_postgres_recommendation(
                snapshot,
                universe,
                repository,
                singleton_profile,
                optimization_plan,
                search_result,
            )
            digest = write_recommendation(recommendation, args.output)
            print(
                json.dumps(
                    {
                        "status": "built",
                        "output": str(args.output),
                        **inspect_recommendation(args.output),
                        "semantic_digest": digest,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "optimization":
            if args.optimization_command == "inspect":
                print(
                    json.dumps(
                        inspect_optimization_plan(args.optimization_plan), sort_keys=True, indent=2
                    )
                )
                return 0
            if args.optimization_command == "search":
                if args.search_command == "inspect":
                    print(
                        json.dumps(
                            inspect_search_result(args.search_result), sort_keys=True, indent=2
                        )
                    )
                    return 0
                snapshot = load_snapshot(args.snapshot)
                universe = load_candidate_universe(args.candidate_universe, snapshot)
                repository = load_native_stats_repository(args.native_repository)
                ground_truth = load_ground_truth_set(args.ground_truth, snapshot)
                singleton_profile = load_singleton_profile(args.singleton_profile)
                optimization_plan = load_optimization_plan(args.optimization_plan)
                validate_singleton_profile(
                    args.singleton_profile, snapshot, universe, repository, ground_truth
                )
                validate_optimization_plan(
                    args.optimization_plan,
                    snapshot,
                    universe,
                    repository,
                    ground_truth,
                    singleton_profile,
                )
                if args.search_command == "validate":
                    print(
                        json.dumps(
                            {
                                "status": "valid",
                                **validate_search_result(
                                    args.search_result,
                                    snapshot,
                                    universe,
                                    repository,
                                    ground_truth,
                                    singleton_profile,
                                    optimization_plan,
                                ),
                            },
                            sort_keys=True,
                        )
                    )
                    return 0
                if not args.dsn:
                    raise ExtStatsAdvisorError(
                        "patched PostgreSQL DSN is required via --dsn or "
                        "EXTSTATS_ADVISOR_PATCHED_POSTGRES_DSN"
                    )
                utility_provider = WeightedWorkloadUtility(
                    snapshot.workload,
                    ArtifactGroundTruthProvider(ground_truth),
                    QErrorLoss(),
                )
                with PostgresPlannerSession(
                    args.dsn, snapshot, universe, repository
                ) as planner_session:
                    result = search_postgres_greedy_add(
                        planner_session,
                        snapshot,
                        universe,
                        repository,
                        singleton_profile,
                        optimization_plan,
                        utility_provider,
                    )
                digest = write_search_result(result, args.output)
                print(
                    json.dumps(
                        {
                            "status": "searched",
                            "output": str(args.output),
                            **inspect_search_result(args.output),
                            "semantic_digest": digest,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            snapshot = load_snapshot(args.snapshot)
            universe = load_candidate_universe(args.candidate_universe, snapshot)
            repository = load_native_stats_repository(args.native_repository)
            ground_truth = load_ground_truth_set(args.ground_truth, snapshot)
            singleton_profile = load_singleton_profile(args.singleton_profile)
            validate_singleton_profile(
                args.singleton_profile, snapshot, universe, repository, ground_truth
            )
            if args.optimization_command == "validate":
                print(
                    json.dumps(
                        {
                            "status": "valid",
                            **validate_optimization_plan(
                                args.optimization_plan,
                                snapshot,
                                universe,
                                repository,
                                ground_truth,
                                singleton_profile,
                            ),
                        },
                        sort_keys=True,
                    )
                )
                return 0
            plan = create_optimization_plan(
                singleton_profile,
                candidate_limit=args.candidate_limit,
                max_statistics_count=(
                    args.max_statistics_count
                    if args.max_statistics_count is not None
                    else args.candidate_limit
                ),
                wall_clock_seconds=args.wall_clock_seconds,
            )
            digest = write_optimization_plan(plan, args.output)
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "output": str(args.output),
                        **inspect_optimization_plan(args.output),
                        "semantic_digest": digest,
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
