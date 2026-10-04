"""DBMS acquisition boundaries; concrete backends are added later."""

from extstats_advisor.dbms.base import AcquisitionInputs, DBMSAcquirer, SampleRequest

__all__ = ["AcquisitionInputs", "DBMSAcquirer", "SampleRequest"]
