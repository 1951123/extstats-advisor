from __future__ import annotations

import inspect

from extstats_advisor.dbms.base import (
    AcquisitionInputs,
    AcquisitionRequest,
    DBMSAcquirer,
    SamplePolicy,
)


def test_dbms_contract_is_small_and_backend_neutral() -> None:
    assert inspect.signature(DBMSAcquirer.acquire).parameters.keys() == {
        "self",
        "request",
    }
    request = AcquisitionRequest("public.events", SamplePolicy(1000, seed=7))
    assert request.relation_selector == "public.events"
    assert request.sampling.seed == 7
    assert AcquisitionInputs.__dataclass_fields__["samples"].type == "Mapping[str, Any]"
