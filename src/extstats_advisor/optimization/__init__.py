"""Optimization contracts that precede future screening and search."""

from extstats_advisor.optimization.artifact import (
    inspect_singleton_profile,
    load_singleton_profile,
    validate_singleton_profile,
    write_singleton_profile,
)
from extstats_advisor.optimization.singleton import (
    SINGLETON_PRECEDENCE_POLICY,
    SINGLETON_PROFILE_FORMAT_VERSION,
    BaselineProfile,
    CandidateSingletonProfile,
    SingletonProfile,
    profile_singletons,
)

__all__ = [
    "SINGLETON_PRECEDENCE_POLICY",
    "SINGLETON_PROFILE_FORMAT_VERSION",
    "BaselineProfile",
    "CandidateSingletonProfile",
    "SingletonProfile",
    "inspect_singleton_profile",
    "load_singleton_profile",
    "profile_singletons",
    "validate_singleton_profile",
    "write_singleton_profile",
]
