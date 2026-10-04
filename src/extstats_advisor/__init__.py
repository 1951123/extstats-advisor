"""Production-oriented extended-statistics advisor bootstrap."""

__version__ = "0.1.0"

from extstats_advisor.snapshot.bundle import load_snapshot, validate_snapshot, write_snapshot
from extstats_advisor.snapshot.model import AdvisorSnapshot

__all__ = ["AdvisorSnapshot", "__version__", "load_snapshot", "validate_snapshot", "write_snapshot"]
