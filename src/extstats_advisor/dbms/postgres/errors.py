"""Expected failure categories for PostgreSQL snapshot acquisition."""

from extstats_advisor.errors import ExtStatsAdvisorError


class PostgresAcquisitionError(ExtStatsAdvisorError):
    """Base class for expected PostgreSQL acquisition failures."""


class PostgresConnectionError(PostgresAcquisitionError):
    """The PostgreSQL driver could not establish or verify a session."""


class RelationNotFoundError(PostgresAcquisitionError):
    """The native PostgreSQL relation selector resolved to no relation."""


class UnsupportedRelationError(PostgresAcquisitionError):
    """The relation is not a supported ordinary stored base table."""


class PostgresPermissionError(PostgresAcquisitionError):
    """The capture role cannot safely inspect the requested relation."""


class UnsupportedPostgresTypeError(PostgresAcquisitionError):
    """A relation column has no lossless v1 Arrow mapping."""


class InvalidPopulationEstimateError(PostgresAcquisitionError):
    """The PostgreSQL population estimate is absent or not usable."""


class SamplingResourceLimitError(PostgresAcquisitionError):
    """A sampling attempt exceeded its configured bounded candidate budget."""


class SampleAcquisitionError(PostgresAcquisitionError):
    """The source rows could not be streamed into a typed sample."""
