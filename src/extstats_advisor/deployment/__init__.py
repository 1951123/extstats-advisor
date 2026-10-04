"""Generic deployment result models and artifact operations."""

from extstats_advisor.deployment.artifact import (
    inspect_deployment_result,
    load_deployment_result,
    validate_deployment_result,
    write_deployment_result,
)
from extstats_advisor.deployment.model import (
    DEPLOYMENT_POLICY,
    DEPLOYMENT_RESULT_FORMAT_VERSION,
    DeployedObject,
    DeploymentResult,
)

__all__ = [
    "DEPLOYMENT_POLICY",
    "DEPLOYMENT_RESULT_FORMAT_VERSION",
    "DeployedObject",
    "DeploymentResult",
    "inspect_deployment_result",
    "load_deployment_result",
    "validate_deployment_result",
    "write_deployment_result",
]
