"""Pure PostgreSQL deployment-recommendation construction and SQL rendering."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from extstats_advisor.canonical import digest_json
from extstats_advisor.errors import (
    CandidateUniverseValidationError,
    NativeStatsRepositoryValidationError,
    RecommendationError,
    RecommendationValidationError,
)
from extstats_advisor.native_stats.model import PRESENT
from extstats_advisor.native_stats.repository import validate_native_stats_repository_compatibility
from extstats_advisor.optimization.plan import OptimizationPlan
from extstats_advisor.optimization.search import SearchResult
from extstats_advisor.optimization.search_artifact import _validate_search_history
from extstats_advisor.optimization.singleton import SingletonProfile
from extstats_advisor.recommendation.model import (
    DECISION_NO_CHANGE,
    DECISION_PROPOSE_CHANGE,
    POSTGRES_DEPLOYMENT_CONTRACT,
    POSTGRES_NAMING_POLICY,
    POSTGRES_PHYSICAL_ORDER_CONTRACT,
    POSTGRES_STATISTICS_KINDS,
    PRECEDENCE_POLICY,
    PRECEDENCE_SOURCE,
    AnalyzeRelationAction,
    CreateStatisticsAction,
    Recommendation,
    SelectedCandidate,
    SetStatisticsTargetAction,
    StatisticsObject,
    TargetRelation,
)


def postgres_statistics_object_name(candidate_id: str, kind: str) -> str:
    """Return the stable PostgreSQL object name for one candidate identity."""

    prefix = {
        "postgresql.mcv": "extstats_adv_mcv_",
        "postgresql.dependencies": "extstats_adv_dep_",
    }.get(kind)
    if prefix is None:
        raise RecommendationError(f"unsupported PostgreSQL statistics kind: {kind}")
    return f"{prefix}{digest_json({'candidate_id': candidate_id, 'kind': kind})[:40]}"


def _require_digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise RecommendationValidationError(f"{label} is missing a semantic digest")
    return value


def _validate_chain(
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    singleton_profile: SingletonProfile,
    optimization_plan: OptimizationPlan,
    search_result: SearchResult,
) -> None:
    snapshot_digest = _require_digest(snapshot.semantic_digest, "snapshot")
    if candidate_universe.source_snapshot_semantic_digest != snapshot_digest:
        raise RecommendationValidationError("candidate universe snapshot digest mismatch")
    try:
        validate_native_stats_repository_compatibility(
            native_repository, snapshot, candidate_universe
        )
    except (NativeStatsRepositoryValidationError, CandidateUniverseValidationError) as exc:
        raise RecommendationValidationError(
            "native repository is incompatible with sources"
        ) from exc
    expected_digests = (
        (
            "singleton profile snapshot",
            singleton_profile.source_snapshot_semantic_digest,
            snapshot_digest,
        ),
        (
            "singleton profile candidate universe",
            singleton_profile.candidate_universe_semantic_digest,
            candidate_universe.semantic_digest,
        ),
        (
            "singleton profile native repository",
            singleton_profile.native_stats_repository_semantic_digest,
            native_repository.semantic_digest,
        ),
        (
            "optimization plan singleton profile",
            optimization_plan.singleton_profile_semantic_digest,
            singleton_profile.computed_semantic_digest,
        ),
        (
            "search result singleton profile",
            search_result.singleton_profile_semantic_digest,
            singleton_profile.computed_semantic_digest,
        ),
        (
            "search result optimization plan",
            search_result.optimization_plan_semantic_digest,
            optimization_plan.computed_semantic_digest,
        ),
        ("search result snapshot", search_result.source_snapshot_semantic_digest, snapshot_digest),
        (
            "search result candidate universe",
            search_result.candidate_universe_semantic_digest,
            candidate_universe.semantic_digest,
        ),
        (
            "search result native repository",
            search_result.native_stats_repository_semantic_digest,
            native_repository.semantic_digest,
        ),
    )
    for label, actual, expected in expected_digests:
        if actual != expected:
            raise RecommendationValidationError(f"{label} digest mismatch")
    if optimization_plan.source_snapshot_semantic_digest != snapshot_digest:
        raise RecommendationValidationError("optimization plan snapshot digest mismatch")
    if optimization_plan.candidate_universe_semantic_digest != candidate_universe.semantic_digest:
        raise RecommendationValidationError("optimization plan candidate universe digest mismatch")
    if (
        optimization_plan.native_stats_repository_semantic_digest
        != native_repository.semantic_digest
    ):
        raise RecommendationValidationError("optimization plan native repository digest mismatch")
    if (
        optimization_plan.ground_truth_semantic_digest
        != singleton_profile.ground_truth_semantic_digest
    ):
        raise RecommendationValidationError("ground-truth digest chain mismatch")
    if search_result.ground_truth_semantic_digest != singleton_profile.ground_truth_semantic_digest:
        raise RecommendationValidationError("search result ground-truth digest mismatch")
    if (
        search_result.utility_contract != optimization_plan.utility_contract
        or search_result.loss_contract != optimization_plan.loss_contract
    ):
        raise RecommendationValidationError("search utility/loss contract mismatch")
    if search_result.planner_identity.sandbox_contract != singleton_profile.sandbox_contract:
        raise RecommendationValidationError("search planner sandbox identity mismatch")
    if search_result.planner_identity.backend_contract != singleton_profile.backend_contract:
        raise RecommendationValidationError("search planner backend identity mismatch")
    if search_result.planner_identity.server_version != singleton_profile.server_version:
        raise RecommendationValidationError("search planner server identity mismatch")
    if search_result.planner_identity.server_version_num != singleton_profile.server_version_num:
        raise RecommendationValidationError("search planner server number mismatch")
    if (
        search_result.planner_identity.ordinary_stats_fingerprint
        != singleton_profile.ordinary_stats_fingerprint
    ):
        raise RecommendationValidationError("search planner ordinary-stats identity mismatch")
    if (
        search_result.semantic_digest is not None
        and search_result.semantic_digest != search_result.computed_semantic_digest
    ):
        raise RecommendationValidationError("search result semantic digest mismatch")
    universe_ids = {candidate.candidate_id for candidate in candidate_universe.candidates}
    profile_ids = {candidate.candidate_id for candidate in singleton_profile.candidate_profiles}
    if profile_ids != universe_ids:
        raise RecommendationValidationError(
            "singleton profile candidate universe membership mismatch"
        )
    present_ids = {
        candidate.candidate_id
        for candidate in singleton_profile.candidate_profiles
        if candidate.native_state == PRESENT
    }
    if set(singleton_profile.frozen_ordered_candidate_ids) != present_ids:
        raise RecommendationValidationError(
            "singleton profile frozen order is not the PRESENT candidate set"
        )
    screened_ids = {candidate.candidate_id for candidate in optimization_plan.screened_candidates}
    excluded_ids = set(optimization_plan.excluded_actionable_candidate_ids)
    frozen_ids = set(singleton_profile.frozen_ordered_candidate_ids)
    if screened_ids | excluded_ids != frozen_ids:
        raise RecommendationValidationError(
            "optimization plan candidate membership does not match singleton profile"
        )
    if screened_ids & excluded_ids:
        raise RecommendationValidationError(
            "optimization plan screened and excluded candidates overlap"
        )
    if set(optimization_plan.absent_native_candidate_ids) != universe_ids - frozen_ids:
        raise RecommendationValidationError(
            "optimization plan ABSENT_NATIVE membership does not match singleton profile"
        )
    profile_by_id = {
        candidate.candidate_id: candidate for candidate in singleton_profile.candidate_profiles
    }
    for screened in optimization_plan.screened_candidates:
        profile = profile_by_id.get(screened.candidate_id)
        if profile is None:
            raise RecommendationValidationError("optimization plan contains unknown candidate")
        if (
            screened.singleton_objective != profile.singleton_objective
            or screened.singleton_improvement != profile.improvement
        ):
            raise RecommendationValidationError(
                "optimization plan singleton utility does not match profile"
            )
    try:
        _validate_search_history(search_result, singleton_profile, optimization_plan)
    except RecommendationValidationError:
        raise
    except Exception as exc:
        raise RecommendationValidationError(
            "search result is inconsistent with its sources"
        ) from exc


def _target_relation(snapshot: Any) -> TargetRelation:
    if len(snapshot.schemas) != 1:
        raise RecommendationError("PostgreSQL recommendation v1 requires exactly one relation")
    relation = snapshot.schemas[0]
    relation_name = relation.relation_name
    if relation_name.schema is None:
        raise RecommendationError("PostgreSQL recommendation requires a relation schema")
    return TargetRelation(
        relation.relation_id,
        relation_name.catalog,
        relation_name.schema,
        relation_name.name,
    )


def _candidate_order(
    selected_ids: Iterable[str], singleton_profile: SingletonProfile, plan: OptimizationPlan
) -> tuple[str, ...]:
    selected = tuple(selected_ids)
    if len(selected) != len(set(selected)):
        raise RecommendationValidationError("SearchResult selected membership contains duplicates")
    screened = set(plan.screened_candidate_ids)
    if not set(selected).issubset(screened):
        raise RecommendationValidationError("selected candidate is outside screened search space")
    derived = tuple(
        candidate_id
        for candidate_id in singleton_profile.frozen_ordered_candidate_ids
        if candidate_id in set(selected)
    )
    if derived != selected:
        raise RecommendationValidationError(
            "SearchResult final order differs from SingletonProfile frozen order"
        )
    return derived


def _build_selected_candidates(
    deployment_order: tuple[str, ...],
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    singleton_profile: SingletonProfile,
    target: TargetRelation,
) -> tuple[SelectedCandidate, ...]:
    universe_by_id = {
        candidate.candidate_id: candidate for candidate in candidate_universe.candidates
    }
    repository_by_id = {
        candidate.candidate_id: candidate for candidate in native_repository.candidate_models
    }
    profile_by_id = {
        candidate.candidate_id: candidate for candidate in singleton_profile.candidate_profiles
    }
    relation = snapshot.schema_by_id.get(target.relation_id)
    if relation is None:
        raise RecommendationValidationError("target relation is absent from snapshot")
    columns_by_ordinal = {column.ordinal: column.name for column in relation.columns}
    result = []
    for position, candidate_id in enumerate(deployment_order, start=1):
        candidate = universe_by_id.get(candidate_id)
        repository_candidate = repository_by_id.get(candidate_id)
        profile_candidate = profile_by_id.get(candidate_id)
        if candidate is None or repository_candidate is None or profile_candidate is None:
            raise RecommendationValidationError(
                "selected candidate is missing from a source artifact"
            )
        if repository_candidate.state != PRESENT or profile_candidate.native_state != PRESENT:
            raise RecommendationValidationError(
                "ABSENT_NATIVE candidate cannot enter Recommendation"
            )
        if candidate.kind not in POSTGRES_STATISTICS_KINDS:
            raise RecommendationValidationError("selected candidate kind is unsupported")
        if (
            repository_candidate.relation_id != candidate.relation_id
            or repository_candidate.kind != candidate.kind
            or repository_candidate.column_ordinals != candidate.column_ordinals
            or repository_candidate.column_names != candidate.column_names
        ):
            raise RecommendationValidationError("repository candidate metadata mismatch")
        if candidate.relation_id != target.relation_id:
            raise RecommendationValidationError("selected candidate targets the wrong relation")
        if (
            tuple(columns_by_ordinal.get(ordinal) for ordinal in candidate.column_ordinals)
            != candidate.column_names
        ):
            raise RecommendationValidationError("candidate columns do not match snapshot schema")
        if profile_candidate.frozen_precedence_rank is None:
            raise RecommendationValidationError("selected candidate has no frozen precedence rank")
        object_name = postgres_statistics_object_name(candidate.candidate_id, candidate.kind)
        result.append(
            SelectedCandidate(
                candidate.candidate_id,
                candidate.kind,
                candidate.relation_id,
                candidate.column_ordinals,
                candidate.column_names,
                profile_candidate.frozen_precedence_rank,
                position,
                StatisticsObject(target.schema, object_name),
                native_repository.statistics_target,
            )
        )
    return tuple(result)


def build_postgres_recommendation(
    snapshot: Any,
    candidate_universe: Any,
    native_repository: Any,
    singleton_profile: SingletonProfile,
    optimization_plan: OptimizationPlan,
    search_result: SearchResult,
) -> Recommendation:
    """Build desired PostgreSQL state without connecting or executing SQL."""

    _validate_chain(
        snapshot,
        candidate_universe,
        native_repository,
        singleton_profile,
        optimization_plan,
        search_result,
    )
    if snapshot.dbms.name != "postgresql":
        raise RecommendationError("PostgreSQL recommendation requires a PostgreSQL snapshot")
    target = _target_relation(snapshot)
    deployment_order = _candidate_order(
        search_result.final_ordered_candidate_ids, singleton_profile, optimization_plan
    )
    decision = DECISION_PROPOSE_CHANGE if deployment_order else DECISION_NO_CHANGE
    if deployment_order and not search_result.final_objective < search_result.baseline_objective:
        raise RecommendationValidationError("non-empty recommendation lacks strict improvement")
    selected = _build_selected_candidates(
        deployment_order, snapshot, candidate_universe, native_repository, singleton_profile, target
    )
    actions = []
    for candidate in selected:
        actions.append(
            CreateStatisticsAction(
                candidate.candidate_id,
                candidate.relation_id,
                candidate.statistics_object.schema,
                candidate.statistics_object.name,
                candidate.kind,
                candidate.column_names,
            )
        )
        actions.append(
            SetStatisticsTargetAction(
                candidate.candidate_id,
                candidate.statistics_object.schema,
                candidate.statistics_object.name,
                candidate.statistics_target,
            )
        )
    if selected:
        actions.append(AnalyzeRelationAction(target))
    return Recommendation(
        search_result.source_snapshot_semantic_digest,
        search_result.candidate_universe_semantic_digest,
        search_result.native_stats_repository_semantic_digest,
        search_result.ground_truth_semantic_digest,
        search_result.singleton_profile_semantic_digest,
        search_result.optimization_plan_semantic_digest,
        search_result.computed_semantic_digest,
        snapshot.dbms.name,
        snapshot.dbms.version or native_repository.server_version,
        POSTGRES_DEPLOYMENT_CONTRACT,
        PRECEDENCE_SOURCE,
        PRECEDENCE_POLICY,
        POSTGRES_NAMING_POLICY,
        POSTGRES_PHYSICAL_ORDER_CONTRACT,
        search_result.search_policy,
        search_result.termination_reason,
        search_result.baseline_objective,
        search_result.final_objective,
        search_result.improvement,
        decision,
        target,
        deployment_order,
        deployment_order,
        selected,
        tuple(actions),
    )


def quote_postgresql_identifier(value: str) -> str:
    """Quote one PostgreSQL identifier without interpreting dots or SQL."""

    if not isinstance(value, str) or not value:
        raise RecommendationValidationError("PostgreSQL identifier must be non-empty")
    return '"' + value.replace('"', '""') + '"'


def render_postgres_sql(recommendation: Recommendation) -> str:
    """Render the structured recommendation as deterministic review-only SQL."""

    lines: list[str] = []
    for action in recommendation.ddl_plan:
        if isinstance(action, CreateStatisticsAction):
            kind = POSTGRES_STATISTICS_KINDS[action.statistics_kind]
            columns = ", ".join(quote_postgresql_identifier(name) for name in action.column_names)
            lines.extend(
                (
                    (
                        f"CREATE STATISTICS {quote_postgresql_identifier(action.object_schema)}."
                        f"{quote_postgresql_identifier(action.object_name)}"
                    ),
                    f"({kind})",
                    f"ON {columns}",
                    (
                        f"FROM {quote_postgresql_identifier(recommendation.target_relation.schema)}."
                        f"{quote_postgresql_identifier(recommendation.target_relation.name)};"
                    ),
                    "",
                )
            )
        elif isinstance(action, SetStatisticsTargetAction):
            lines.extend(
                (
                    (
                        f"ALTER STATISTICS {quote_postgresql_identifier(action.object_schema)}."
                        f"{quote_postgresql_identifier(action.object_name)}"
                    ),
                    f"SET STATISTICS {action.statistics_target};",
                    "",
                )
            )
        elif isinstance(action, AnalyzeRelationAction):
            lines.append(
                f"ANALYZE {quote_postgresql_identifier(action.relation.schema)}."
                f"{quote_postgresql_identifier(action.relation.name)};"
            )
        else:
            raise RecommendationValidationError("unknown PostgreSQL DDL action")
    return "\n".join(lines).rstrip() + ("\n" if lines else "")


__all__ = [
    "build_postgres_recommendation",
    "postgres_statistics_object_name",
    "quote_postgresql_identifier",
    "render_postgres_sql",
]
