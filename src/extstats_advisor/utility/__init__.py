"""DBMS-neutral cardinality loss and workload utility contracts."""

from extstats_advisor.utility.loss import (
    QERROR_CONTRACT_VERSION,
    CardinalityLoss,
    QErrorLoss,
)
from extstats_advisor.utility.model import PerQueryUtility, UtilityResult
from extstats_advisor.utility.provider import (
    UTILITY_CONTRACT_VERSION,
    UtilityProvider,
    WeightedWorkloadUtility,
)

__all__ = [
    "QERROR_CONTRACT_VERSION",
    "UTILITY_CONTRACT_VERSION",
    "CardinalityLoss",
    "PerQueryUtility",
    "QErrorLoss",
    "UtilityProvider",
    "UtilityResult",
    "WeightedWorkloadUtility",
]
