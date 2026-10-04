"""Recommendation models and lazily loaded artifact operations."""

from extstats_advisor.recommendation.model import (
    DECISION_NO_CHANGE,
    DECISION_PROPOSE_CHANGE,
    POSTGRES_DEPLOYMENT_CONTRACT,
    POSTGRES_NAMING_POLICY,
    POSTGRES_PHYSICAL_ORDER_CONTRACT,
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


def inspect_recommendation(*args: object, **kwargs: object):
    from extstats_advisor.recommendation.artifact import inspect_recommendation as inspect

    return inspect(*args, **kwargs)


def load_recommendation(*args: object, **kwargs: object):
    from extstats_advisor.recommendation.artifact import load_recommendation as load

    return load(*args, **kwargs)


def validate_recommendation(*args: object, **kwargs: object):
    from extstats_advisor.recommendation.artifact import validate_recommendation as validate

    return validate(*args, **kwargs)


def write_recommendation(*args: object, **kwargs: object):
    from extstats_advisor.recommendation.artifact import write_recommendation as write

    return write(*args, **kwargs)


__all__ = [
    "DECISION_NO_CHANGE",
    "DECISION_PROPOSE_CHANGE",
    "POSTGRES_DEPLOYMENT_CONTRACT",
    "POSTGRES_NAMING_POLICY",
    "POSTGRES_PHYSICAL_ORDER_CONTRACT",
    "PRECEDENCE_POLICY",
    "PRECEDENCE_SOURCE",
    "AnalyzeRelationAction",
    "CreateStatisticsAction",
    "Recommendation",
    "SelectedCandidate",
    "SetStatisticsTargetAction",
    "StatisticsObject",
    "TargetRelation",
    "inspect_recommendation",
    "load_recommendation",
    "validate_recommendation",
    "write_recommendation",
]
